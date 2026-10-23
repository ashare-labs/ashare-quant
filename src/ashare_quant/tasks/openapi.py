"""Shared contract generation for runtime endpoints and published snapshots."""


def document(app, *, prefixes=None, title="A宽 research tasks"):
    full = app.openapi()
    paths = {
        name: value
        for name, value in full["paths"].items()
        if name.startswith(prefixes or
            ("/api/v1/tasks/", "/api/v1/python/", "/api/v1/factors/", "/api/v1/history/")
        )
    }
    required = set()

    def collect(value):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                assert ref.startswith("#/components/schemas/")
                required.add(ref.removeprefix("#/components/schemas/"))
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(paths)
    schemas = {}
    while required - schemas.keys():
        name = sorted(required - schemas.keys())[0]
        schemas[name] = full["components"]["schemas"][name]
        collect(schemas[name])
    return {
        "openapi": full["openapi"],
        "info": {"title": title, "version": full["info"]["version"]},
        "paths": paths,
        "components": {"schemas": dict(sorted(schemas.items()))},
    }
