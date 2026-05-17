#!/usr/bin/env python3
"""nanoclaw — AI-powered sketch-to-3D-print pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import print as rprint

load_dotenv()

console = Console()


def _check_api_key() -> None:
    import os
    has_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("USE_OLLAMA", "").lower() in ("1", "true", "yes")
    )
    if not has_key:
        console.print(
            "[bold red]Error:[/] No LLM API key found.\n"
            "Set [cyan]OPENROUTER_API_KEY[/] or [cyan]ANTHROPIC_API_KEY[/] in your .env,\n"
            "or start Ollama and set [cyan]USE_OLLAMA=1[/]."
        )
        sys.exit(1)


@click.group()
def cli() -> None:
    """nanoclaw: sketch / photo / description → 3D printable STL package."""


@cli.command()
@click.option("--description", "-d", default=None, help="Text description of the object.")
@click.option("--image", "-i", default=None, type=click.Path(exists=True), help="Path to a sketch or photo.")
@click.option("--output", "-o", default="./output", show_default=True, help="Output root directory.")
@click.option(
    "--mode", "-m",
    type=click.Choice(["auto", "partgen", "parametric", "mesh"]),
    default="auto",
    show_default=True,
    help="auto | partgen (CadQuery templates) | parametric (OpenSCAD) | mesh (AI mesh generation)",
)
@click.option(
    "--backend", "-b",
    type=click.Choice(["shape-e", "tripo3d", "meshy"]),
    default="shape-e",
    show_default=True,
    help=(
        "Mesh backend (only used when mode=mesh or auto selects mesh).  "
        "shape-e: local/free, no key needed  |  "
        "tripo3d: free API tier with downloads (TRIPO3D_API_KEY)  |  "
        "meshy: paid tier required (MESHY_API_KEY)"
    ),
)
@click.option("--analysis-model", default=None, help="Claude model for analysis stage.")
@click.option("--design-model", default=None, help="Claude model for design stage.")
def generate(
    description: str | None,
    image: str | None,
    output: str,
    mode: str,
    backend: str,
    analysis_model: str | None,
    design_model: str | None,
) -> None:
    """Run the full pipeline: analyse → design → partition → package.

    \b
    Modes:
      auto        Choose automatically (organic → mesh, mechanical → parametric)
      parametric  OpenSCAD-based — best for boxes, brackets, functional parts
      mesh        AI mesh — best for characters, animals, sculptures

    \b
    Mesh backends (use with --mode mesh or auto):
      shape-e     Fully local, FREE, no API key, works offline
                  pip install shap-e  (downloads ~1 GB model on first run)
      tripo3d     Cloud API, free tier allows downloads
                  Register at https://platform.tripo3d.ai → TRIPO3D_API_KEY
      meshy       Cloud API, requires paid plan for downloads
                  https://www.meshy.ai → MESHY_API_KEY
    """
    _check_api_key()

    if not description and not image:
        console.print("[bold red]Error:[/] Provide --description and/or --image.")
        sys.exit(1)

    backend_labels = {
        "shape-e": "Shap-E (local/free)",
        "tripo3d": "Tripo3D (API)",
        "meshy": "Meshy.ai (API)",
    }
    mode_label = {
        "auto": "Auto-detect",
        "partgen": "Part-gen (CadQuery templates)",
        "parametric": "Parametric (OpenSCAD)",
        "mesh": f"AI Mesh ({backend_labels[backend]})",
    }.get(mode, mode)

    console.print(
        Panel.fit(
            f"[bold cyan]nanoclaw[/] AI 3D Printing Pipeline\n"
            f"[dim]Mode: {mode_label}[/]",
            subtitle="sketch → STL package",
        )
    )

    from pipeline import Pipeline

    def on_progress(stage: str, message: str) -> None:
        icons = {
            "analyse": "🔍", "mode": "⚙️", "design": "✏️",
            "partition": "✂️", "package": "📦",
        }
        icon = icons.get(stage, "•")
        console.print(f"  {icon}  [bold]{stage.capitalize()}:[/] {message}")

    try:
        pipeline = Pipeline(
            output_root=output,
            mesh_backend=backend,
            mode=mode,
            analysis_model=analysis_model,
            design_model=design_model,
        )
        result = pipeline.run(
            text_description=description,
            image_path=image,
            progress_callback=on_progress,
        )
    except Exception as exc:
        console.print(f"\n[bold red]Pipeline failed:[/] {exc}")
        raise SystemExit(1) from exc

    _print_summary(result)


def _print_summary(result) -> None:
    from rich.tree import Tree

    obj = result.object_description
    pkg = result.package
    mode = getattr(result, "mode_used", "parametric")

    console.print()
    console.print(
        Panel.fit(
            f"[bold green]Done![/] {obj.name}",
            subtitle=f"Mode: {mode} | Package Summary",
        )
    )

    # Parts table
    table = Table(title="Parts", show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Module")
    table.add_column("SCAD")
    table.add_column("STL")
    table.add_column("Printable")

    for part in result.partition.parts:
        has_stl = "✓" if part.stl_path and part.stl_path.exists() else "—"
        printable = "[green]Yes[/]" if part.printable else "[red]No[/]"
        table.add_row(
            str(part.index),
            part.module_name,
            part.scad_filename,
            part.stl_filename if has_stl == "✓" else "render needed",
            printable,
        )
    console.print(table)

    # Output tree
    tree = Tree(f"[bold]{pkg.output_dir}[/]")
    tree.add("[dim]scad/[/]  — OpenSCAD source files")
    tree.add("[dim]stl/[/]   — STL files (if OpenSCAD installed)")
    tree.add("[dim]docs/[/]  — print_instructions.md, assembly_guide.md")
    tree.add("[dim]manifest.json[/]")
    console.print(tree)

    if result.partition.warnings:
        console.print("\n[yellow]Warnings:[/]")
        for w in result.partition.warnings:
            console.print(f"  • {w}")

    console.print(
        f"\n[dim]STL files: {pkg.stl_count}  |  SCAD files: {pkg.scad_count}[/]"
    )
    if pkg.stl_count == 0:
        console.print(
            "[yellow]No STL files rendered.[/] "
            "Install [bold]OpenSCAD[/] and run:\n"
            f"  openscad -o <part>.stl scad/<part>.scad\n"
            f"or open SCAD files in OpenSCAD GUI."
        )


@cli.command()
@click.option("--port", "-p", default=7860, show_default=True, help="Port to listen on.")
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload (development).")
def serve(port: int, host: str, reload: bool) -> None:
    """Start the localhost web UI.

    \b
    Then open  http://localhost:<port>  in your browser.
    """
    try:
        import uvicorn
    except ImportError:
        console.print("[bold red]Error:[/] uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    console.print(
        Panel.fit(
            f"[bold cyan]nanoclaw[/] Web UI\n"
            f"[dim]Open [bold]http://localhost:{port}[/] in your browser[/]",
        )
    )
    uvicorn.run("ui.app:app", host=host, port=port, reload=reload)


@cli.command()
@click.option("--setup", is_flag=True, default=False, help="First-time setup: set password and configure 2FA.")
@click.option("--port", "-p", default=7861, show_default=True, help="Port for the remote server.")
@click.option("--no-tunnel", is_flag=True, default=False, help="Skip Cloudflare Tunnel (LAN only).")
def remote(setup: bool, port: int, no_tunnel: bool) -> None:
    """Secure remote desktop — view and control this Mac from any browser.

    \b
    First time:
      python main.py remote --setup

    \b
    Every time after:
      python main.py remote
      Then open the printed URL on your phone/iPad/laptop.

    \b
    For internet access install cloudflared (free):
      brew install cloudflared        (Mac)
      # or download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
    """
    if setup:
        _remote_setup()
    else:
        _remote_serve(port, no_tunnel)


def _remote_setup() -> None:
    from remote import auth as rauth

    console.print(Panel.fit("[bold cyan]Nanoclaw Remote — First-time Setup[/]"))

    if rauth.is_setup_done():
        if not click.confirm("Setup already exists. Overwrite?", default=False):
            return

    ntfy_topic = click.prompt(
        "ntfy.sh topic (unique name — push notifications with your URL)",
        default=f"nanoclaw-{__import__('secrets').token_hex(4)}",
    )
    console.print(
        f"  [dim]Install the free ntfy app and subscribe to: [cyan]ntfy.sh/{ntfy_topic}[/][/]"
    )

    password = click.prompt("Choose a password", hide_input=True, confirmation_prompt=True)

    console.print("\nGenerating TOTP secret…")
    qr_uri, totp_secret, prov_uri = rauth.run_setup(password, ntfy_topic)

    # Print ASCII QR code directly in terminal
    try:
        import qrcode as qr_lib
        qr = qr_lib.QRCode()
        qr.add_data(prov_uri)
        qr.make()
        console.print("\n[bold]Scan this QR code with Google Authenticator / Authy:[/]\n")
        qr.print_ascii(invert=True)
    except Exception:
        console.print(f"\n[dim]Provisioning URI (paste into authenticator app):[/]\n{prov_uri}\n")

    console.print(f"\n[dim]Backup TOTP secret: [cyan]{totp_secret}[/][/]")

    # Verify the code works before finishing
    code = click.prompt("\nEnter the 6-digit code from your authenticator to confirm")
    import pyotp
    if not pyotp.TOTP(totp_secret).verify(code.strip(), valid_window=1):
        console.print("[bold red]Code invalid.[/] Run setup again.")
        rauth.CONFIG_PATH.unlink(missing_ok=True)
        return

    console.print(
        Panel.fit(
            f"[bold green]Setup complete![/]\n"
            f"ntfy topic: [cyan]{ntfy_topic}[/]\n\n"
            "Run [bold]python main.py remote[/] to start.",
        )
    )


def _remote_serve(port: int, no_tunnel: bool) -> None:
    import subprocess
    import threading
    import re
    import httpx

    from remote import auth as rauth

    if not rauth.is_setup_done():
        console.print(
            "[bold red]Not configured.[/] Run first: [cyan]python main.py remote --setup[/]"
        )
        return

    try:
        import uvicorn
    except ImportError:
        console.print("[bold red]uvicorn not installed.[/] Run: pip install uvicorn")
        return

    local_url = f"http://localhost:{port}"
    console.print(
        Panel.fit(
            f"[bold cyan]Nanoclaw Remote[/]\n"
            f"[dim]Local: [bold]{local_url}[/][/]",
        )
    )

    def _send_ntfy(public_url: str) -> None:
        topic = rauth.get_ntfy_topic()
        if not topic:
            return
        try:
            httpx.post(
                f"https://ntfy.sh/{topic}",
                content=f"Nanoclaw Remote is live: {public_url}",
                headers={
                    "Title": "Nanoclaw Remote",
                    "Priority": "default",
                    "Tags": "computer",
                },
                timeout=10,
            )
        except Exception:
            pass  # notification is best-effort

    def _start_tunnel() -> None:
        try:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", local_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            url_pattern = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
            for line in proc.stdout:
                m = url_pattern.search(line)
                if m:
                    public_url = m.group(0)
                    console.print(
                        f"\n[bold green]Public URL:[/] [cyan]{public_url}[/]\n"
                        f"[dim]Sending push notification…[/]"
                    )
                    _send_ntfy(public_url)
                    break
        except FileNotFoundError:
            console.print(
                "[yellow]cloudflared not found — LAN access only.[/]\n"
                "[dim]Install: brew install cloudflared[/]"
            )

    if not no_tunnel:
        t = threading.Thread(target=_start_tunnel, daemon=True)
        t.start()

    console.print(
        "[dim]macOS permissions needed on first run:[/]\n"
        "  • Screen Recording  (System Settings → Privacy → Screen Recording)\n"
        "  • Accessibility     (System Settings → Privacy → Accessibility)\n"
    )

    uvicorn.run("remote.server:app", host="0.0.0.0", port=port, log_level="warning")


@cli.command()
@click.argument("image_or_description")
def analyse(image_or_description: str) -> None:
    """Quick-analyse an image or description and print the structured spec (no design)."""
    import json
    import os
    from pathlib import Path

    _check_api_key()
    from agents import AnalysisAgent

    path = Path(image_or_description)
    is_image = path.exists() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    console.print(f"Analysing {'image' if is_image else 'description'}…")
    agent = AnalysisAgent()
    desc = agent.analyse(
        image_path=image_or_description if is_image else None,
        text_description=None if is_image else image_or_description,
    )

    console.print_json(desc.raw_analysis)


if __name__ == "__main__":
    cli()
