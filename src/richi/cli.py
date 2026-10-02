"""Console entry point and lazy dispatch to the optional map process."""


def run_map(args):
    # Pass explicit settings so both foreground and detached map commands use the
    # same configuration resolver. Import map modules only for map commands.
    from .memory import MemoryError
    if args.action == "serve" and args.no_open:
        raise MemoryError("--no-open is only supported by richi map")
    if args.action != "serve" and args.open:
        raise MemoryError("--open is only supported by richi map serve; richi map opens by default")
    argv = []
    for flag, value in (("--db", args.db), ("--config", args.config), ("--port", args.port)):
        if value is not None:
            argv.extend((flag, str(value)))
    if args.dev:
        argv.append("--dev")
    if args.action == "serve":
        from . import serve
        if args.open:
            argv.append("--open")
        return serve.main(argv) or 0
    from . import launch_map
    if args.no_open:
        argv.append("--no-open")
    return launch_map.main(argv) or 0


def main(argv=None):
    from .memory import main as memory_main
    return memory_main(argv)
