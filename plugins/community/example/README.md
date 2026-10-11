# community.example-cache

The reference community plugin: one Valkey container for the `cache` capability.

```yaml
# boss.deploy.yaml
resources:
  cache:
    capability: cache
    provider: community.example-cache
```

The app gets `CACHE_URL` (by reference, from the app secret vault). The port binds to
127.0.0.1; `public: true` needs an approval.

Test it against a real container:

```bash
python -m boss_engine.platform test plugins/community/example
```

Community plugins are disabled until an operator enables them (Infra › Plugins).
