#!/usr/bin/env python3
"""Valida os overlays contra o padrão de manifests da Metacortex."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
OVERLAYS = ROOT / "manifests" / "overlays"
ENVIRONMENTS = ("dev", "stg", "prod")
REQUIRED_LABELS = {
    "app.kubernetes.io/name",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/part-of",
    "app.kubernetes.io/managed-by",
}
KEBAB_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAMESPACE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-(dev|stg|prod)$")
SENSITIVE_NAME = re.compile(r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|DATABASE_URL|PRIVATE_KEY)", re.IGNORECASE)


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, condition: bool, location: str, message: str) -> None:
        if not condition:
            self.errors.append(f"{location}: {message}")


def render(environment: str) -> str:
    overlay = OVERLAYS / environment
    if kubectl := shutil.which("kubectl"):
        command = [kubectl, "kustomize", str(overlay)]
    elif kustomize := shutil.which("kustomize"):
        command = [kustomize, "build", str(overlay)]
    else:
        raise RuntimeError("kubectl ou kustomize não encontrado")
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def pod_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    kind = document.get("kind")
    spec = document.get("spec", {})
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}:
        return [spec.get("template", {}).get("spec", {})]
    if kind == "CronJob":
        return [spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})]
    if kind == "Pod":
        return [spec]
    return []


def validate_container(container: dict[str, Any], location: str, check: Validation) -> None:
    image = str(container.get("image", ""))
    resources = container.get("resources", {})
    context = container.get("securityContext", {})
    check.require(bool(container.get("name")), location, "container sem nome")
    check.require(image.startswith("registry.metacortex.io/"), location, "imagem fora do registry interno")
    check.require(not image.endswith(":latest") and ":latest@" not in image, location, "tag latest é proibida")
    for section in ("requests", "limits"):
        values = resources.get(section, {})
        for resource in ("cpu", "memory"):
            check.require(bool(values.get(resource)), location, f"resources.{section}.{resource} ausente")
    for probe in ("readinessProbe", "livenessProbe"):
        check.require(bool(container.get(probe)), location, f"{probe} ausente")
    expected_context = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
    }
    for key, expected in expected_context.items():
        check.require(context.get(key) == expected, location, f"securityContext.{key} deve ser {expected!r}")
    dropped = context.get("capabilities", {}).get("drop", [])
    check.require("ALL" in dropped, location, "securityContext.capabilities deve remover ALL")
    check.require(context.get("privileged") is not True, location, "privileged é proibido")
    for variable in container.get("env", []):
        if SENSITIVE_NAME.search(str(variable.get("name", ""))):
            reference = variable.get("valueFrom", {}).get("secretKeyRef", {})
            check.require(bool(reference.get("name") and reference.get("key")), location, f"{variable.get('name')} deve usar secretKeyRef")


def validate_environment(environment: str, documents: list[dict[str, Any]], check: Validation) -> None:
    expected_namespace = f"nyx-{environment}"
    deployments: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    pdbs: list[dict[str, Any]] = []

    for document in documents:
        kind = document.get("kind", "<sem-kind>")
        metadata = document.get("metadata", {})
        name = str(metadata.get("name", ""))
        location = f"{environment}/{kind}/{name or '<sem-nome>'}"
        labels = metadata.get("labels", {})

        check.require(bool(KEBAB_CASE.fullmatch(name)), location, "nome não está em kebab-case")
        check.require(REQUIRED_LABELS <= labels.keys(), location, "faltam labels obrigatórios")
        check.require(labels.get("app.kubernetes.io/instance") == expected_namespace, location, "label instance incorreto")
        check.require(labels.get("app.kubernetes.io/managed-by") in {"platform", "argocd", "helm"}, location, "managed-by inválido")
        check.require(bool(metadata.get("annotations", {}).get("metacortex.io/owner")), location, "anotação owner ausente")

        if kind == "Secret":
            check.require(not document.get("data") and not document.get("stringData"), location, "segredo versionado no manifesto")
        if kind == "ConfigMap":
            for key in document.get("data", {}):
                check.require(not SENSITIVE_NAME.search(str(key)), location, f"chave sensível {key} não pode estar em ConfigMap")

        if kind == "Namespace":
            check.require(name == expected_namespace and bool(NAMESPACE.fullmatch(name)), location, "namespace inválido")
        else:
            check.require(metadata.get("namespace") == expected_namespace, location, "namespace ausente ou incorreto")

        if kind == "Deployment":
            deployments.append(document)
        elif kind == "Service":
            services.append(document)
        elif kind == "PodDisruptionBudget":
            pdbs.append(document)

        for pod_spec in pod_specs(document):
            check.require(pod_spec.get("automountServiceAccountToken") is False, location, "automountServiceAccountToken deve ser false")
            check.require(pod_spec.get("serviceAccountName") not in (None, "", "default"), location, "ServiceAccount dedicada ausente")
            check.require(pod_spec.get("hostNetwork") is not True, location, "hostNetwork é proibido")
            check.require(pod_spec.get("hostPID") is not True, location, "hostPID é proibido")
            pod_context = pod_spec.get("securityContext", {})
            check.require(pod_context.get("runAsNonRoot") is True, location, "Pod deve executar como usuário não-root")
            check.require(pod_context.get("seccompProfile", {}).get("type") == "RuntimeDefault", location, "seccompProfile deve ser RuntimeDefault")
            for container in pod_spec.get("initContainers", []) + pod_spec.get("containers", []):
                validate_container(container, f"{location}/container/{container.get('name', '<sem-nome>')}", check)

    for deployment in deployments:
        name = deployment["metadata"]["name"]
        location = f"{environment}/Deployment/{name}"
        spec = deployment.get("spec", {})
        pod_labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        check.require(spec.get("selector", {}).get("matchLabels") == pod_labels, location, "matchLabels deve ser idêntico aos labels do Pod")
        if environment == "prod":
            check.require(spec.get("replicas", 0) >= 2, location, "produção exige ao menos duas réplicas")
            rolling = spec.get("strategy", {})
            check.require(rolling.get("type") == "RollingUpdate", location, "strategy deve ser RollingUpdate")
            update = rolling.get("rollingUpdate", {})
            check.require(update.get("maxUnavailable") == 0, location, "maxUnavailable deve ser 0")
            check.require(update.get("maxSurge") == 1, location, "maxSurge deve ser 1")

        matching_services = [service for service in services if service.get("spec", {}).get("selector") == pod_labels]
        check.require(bool(matching_services), location, "nenhum Service possui seletor idêntico aos labels do Pod")

    if environment == "prod":
        check.require(bool(pdbs), "prod", "PodDisruptionBudget recomendado ausente")
        for pdb in pdbs:
            check.require(pdb.get("spec", {}).get("minAvailable", 0) >= 1, "prod/PodDisruptionBudget", "minAvailable deve ser >= 1")


def main() -> int:
    check = Validation()
    rendered: dict[str, str] = {}
    try:
        for environment in ENVIRONMENTS:
            rendered[environment] = render(environment)
            documents = [item for item in yaml.safe_load_all(rendered[environment]) if item]
            validate_environment(environment, documents, check)
    except (OSError, RuntimeError, yaml.YAMLError) as error:
        print(f"ERRO: não foi possível renderizar os manifests: {error}", file=sys.stderr)
        return 2

    if check.errors:
        print("Manifestos inválidos:", file=sys.stderr)
        for error in check.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    total = sum(len([doc for doc in yaml.safe_load_all(text) if doc]) for text in rendered.values())
    print(f"OK: {total} recursos renderizados nos ambientes dev, stg e prod estão conformes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
