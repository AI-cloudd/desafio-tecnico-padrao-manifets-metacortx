.PHONY: validate scan render-prod

validate:
	python3 scripts/validate.py

scan:
	trivy config manifests

render-prod:
	kubectl kustomize manifests/overlays/prod
