# Padrão de manifests da Metacortex

Exemplo executável de uma aplicação Kubernetes (`nyx-api`) em conformidade com o
padrão interno da Metacortex. Os manifests usam Kustomize para manter uma base
única e overlays para `dev`, `stg` e `prod`.

## Estrutura

```text
manifests/
├── base/                  # Deployment, Service, ConfigMap e ServiceAccount
└── overlays/
    ├── dev/               # namespace nyx-dev, uma réplica
    ├── stg/               # namespace nyx-stg, uma réplica
    └── prod/              # namespace nyx-prod, duas réplicas e PDB
scripts/validate.py        # valida as regras do padrão no YAML renderizado
```

## Pré-requisitos

- Python 3 com PyYAML
- `kubectl` ou `kustomize`
- Trivy (opcional, para a varredura de configuração)

## Validar

```bash
make validate
make scan
```

O primeiro comando renderiza os três overlays, valida a sintaxe com o cliente
Kubernetes e verifica nomenclatura, namespace, labels, seletores, réplicas,
estratégia, probes, recursos, imagem, security context, token de ServiceAccount,
campos de host e PDB de produção. O segundo executa o scanner de configuração do
pipeline, quando o Trivy está instalado.

Para inspecionar ou aplicar um ambiente:

```bash
kubectl kustomize manifests/overlays/prod
kubectl apply -k manifests/overlays/prod
```

> A aplicação espera que o Secret `nyx-db`, chave `url`, já exista no namespace.
> Ele é deliberadamente gerenciado fora deste repositório para que nenhum segredo
> seja versionado. Antes de aplicar, substitua a imagem de exemplo pelo digest/tag
> imutável publicado no registry interno e confirme que `/health/ready` e
> `/health/live` existem na aplicação.

## Decisões de conformidade

- Os quatro labels obrigatórios são propagados também aos seletores.
- A imagem vem exclusivamente de `registry.metacortex.io` e não usa `latest`.
- O filesystem raiz é somente leitura; `/tmp` é um `emptyDir` gravável.
- O token da ServiceAccount não é montado no Pod nem na conta dedicada.
- Produção usa `RollingUpdate`, `maxUnavailable: 0`, `maxSurge: 1`, duas réplicas
  e `PodDisruptionBudget` com `minAvailable: 1`.
- Anotações de owner/runbook, ServiceAccount dedicada e grace period explícito
  implementam também as recomendações aplicáveis.
