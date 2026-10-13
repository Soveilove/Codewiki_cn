"""
Main CLI application for CodeWiki using Click framework.
"""

import sys
import click

from codewiki import __version__
from codewiki.cli.commands.config import config_group
from codewiki.cli.commands.generate import generate_command
from codewiki.cli.utils.branding import print_banner


def _force_utf8_output() -> None:
    """Make the CLI's own output safe on non-UTF-8 consoles.

    Chinese Windows consoles still default to GBK, and progress marks such as
    ``✓`` or ``→`` cannot be encoded there: printing one raised
    ``UnicodeEncodeError`` and turned a finished command into a failure trace.
    Replacing unencodable characters is preferable to losing a long run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="CodeWiki CLI")
@click.pass_context
def cli(ctx):
    """
    CodeWiki: repository-level documentation for large codebases.

    AI agents write the documentation from your dependency graph. Supports
    Python, Java, JavaScript, TypeScript, C, C++, C#, Kotlin, PHP, Ruby,
    Scala, Rust, and Dart.
    """
    _force_utf8_output()
    # Ensure context object exists
    ctx.ensure_object(dict)

    # Bare `codewiki`: show the branded banner, then the usual help text.
    if ctx.invoked_subcommand is None:
        print_banner()
        click.echo(ctx.get_help())
        ctx.exit(0)


@cli.command()
def version():
    """Display version information."""
    print_banner(force=True)
    click.echo(f"CodeWiki CLI v{__version__}")
    click.echo("基于依赖关系图，为大型代码仓库生成可追溯的项目文档。")


# Register command groups
cli.add_command(config_group)
cli.add_command(generate_command, name="generate")


@cli.command(name="mcp")
def mcp_command():
    """Start CodeWiki as an MCP (Model Context Protocol) server.

    Exposes documentation generation tools via MCP stdio transport.
    Configure in your MCP client (Claude, Cursor, etc.) as:

    \b
    {
        "mcpServers": {
            "codewiki": {
                "command": "codewiki",
                "args": ["mcp"]
            }
        }
    }
    """
    import asyncio
    from codewiki.mcp.server import main as mcp_main

    asyncio.run(mcp_main())


def main():
    """Entry point for the CLI."""
    try:
        cli(obj={})
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted by user", err=True)
        sys.exit(130)
    except Exception as e:
        click.secho(f"\n✗ Unexpected error: {e}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
