"""Install routes through the native app, including Hermes's late plugin rewiring."""


def install(app, routes, close):
    from aiohttp import web
    from aiosignal import Signal

    router = web.UrlDispatcher()
    fallback = []
    for resource in app.router.resources():
        # Hermes installs its profile ingress catch-all before calling plugin factories.
        # Keep native routes first, but let the explicit plugin mirrors reach dispatch.
        if resource.canonical == "/p/{profile}/{tail}":
            fallback.append(resource)
        else:
            router.register_resource(resource)
    for resource in (*routes.resources(), *fallback):
        router.register_resource(resource)

    cleanup = Signal(app)
    cleanup.extend(app.on_cleanup)
    cleanup.append(close)
    if app.router.frozen:
        router.freeze()
    if app.on_cleanup.frozen:
        cleanup.freeze()

    # aiohttp has no public API for adding routes or cleanup callbacks after startup.
    # Hermes calls factories synchronously on the gateway loop. Build both registries
    # before publishing them, retaining native resource/callback identities and their
    # frozen state. Existing requests keep their resolved routes and middleware.
    app._router = router
    app._on_cleanup = cleanup
