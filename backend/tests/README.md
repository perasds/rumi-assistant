# Backend tests

Run from `backend` with the project's test dependencies installed:

```sh
python -m pip install -e ".[test]"
python -m pytest
```

Default tests use this checkout's `src`, bundled protocol descriptors and factual configuration, and synthetic account/HTTP/image fixtures. They do not send game login, SMS, or purchase requests. `test_portable_source.py` rejects an accidental import from a different checkout. Each run gets a unique directory under the operating system's temporary directory, unless `--basetemp` is explicitly supplied.

## Optional material checks

Tests marked `external_materials` verify original image bytes, full encyclopedia snapshots or research-report hashes. Those materials are excluded from this repository, and these checks are skipped by default. Skipping them does not claim that the original materials have been verified.

To run only these checks against materials you separately provide:

```sh
python -m pytest --external-materials -m external_materials --external-materials-root /path/to/materials
```

To include them in the complete suite, omit `-m external_materials`. If no root is provided, the root defaults to the repository's `external` directory. The material tree follows these original relative paths:

```text
materials/
  docs/
    gold-hot65-verification.public.json
    gold-hot69-verification.public.json
    gold-hot71-verification.public.json
    hot71-runtime-compatibility.public.json
    hot72-runtime-compatibility.public.json
  artifacts/rumi/
    can-food-analysis/order-materials.public.json
    market-season-analysis/lumi-tags-all-species.public.json
    ... referenced projection and hot65 identity reports
  data/
    pets/   # full catalog, names, details, evidence, and original images
    market/ # full items, evidence, and original images
```

Supply the exact referenced reports and assets; opting in with missing or changed material produces a test failure. APK loader, signature, authentication, public-route safety, planner, version-gate and protocol behavior tests remain in the default suite with synthetic fixtures. Mixed provenance tests separate their external hash check from the default contract assertions.
