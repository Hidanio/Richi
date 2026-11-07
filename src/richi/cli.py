"""Lazy dispatch from the selected Richi runtime to the optional map process."""


def run_map(args):
    # Pass explicit settings so both foreground and detached map commands use the
    # same configuration resolver. Import map modules only for map commands.
    from .memory import MemoryError
    if args.action == "serve" and args.no_open:
        raise MemoryError("--no-open is only supported by richi map and richi map restart")
    if args.action == "stop" and (args.open or args.no_open):
        raise MemoryError("--open and --no-open do not apply to richi map stop")
    if args.action == "restart" and args.open and args.no_open:
        raise MemoryError("Use either --open or --no-open")
    if args.action not in {"serve", "restart"} and args.open:
        raise MemoryError("--open is only supported by richi map serve and richi map restart; richi map opens by default")
    argv = [args.action] if args.action in {"stop", "restart"} else []
    for flag, value in (("--db", args.db), ("--config", args.config), ("--port", args.port)):
        if value is not None:
            argv.extend((flag, str(value)))
    if args.action == "serve":
        from . import serve
        if args.open:
            argv.append("--open")
        return serve.main(argv) or 0
    from . import launch_map
    if args.no_open:
        argv.append("--no-open")
    if args.open:
        argv.append("--open")
    return launch_map.main(argv) or 0
