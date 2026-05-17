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
@click.option("--install", is_flag=True, default=False, help="Install auto-start on login (runs once).")
@click.option("--uninstall", is_flag=True, default=False, help="Remove auto-start on login.")
@click.option("--port", "-p", default=7861, show_default=True, help="Port for the remote server.")
@click.option("--no-tunnel", is_flag=True, default=False, help="Skip Cloudflare Tunnel (LAN only).")
def remote(setup: bool, install: bool, uninstall: bool, port: int, no_tunnel: bool) -> None:
    """Secure remote desktop — view and control this Mac from any browser.

    \b
    First time (3 steps, done once at the Mac):
      python main.py remote --setup      # set password + scan QR code
      python main.py remote --install    # auto-start on every login
      (configure Mac: no sleep, screen recording + accessibility permissions)

    \b
    Every day after — nothing to do. Just open your bookmarked URL.
    URL also saved to iCloud Drive and Desktop automatically.

    \b
    Requires cloudflared for internet access (free):
      brew install cloudflared
    """
    if setup:
        _remote_setup()
    elif install:
        _remote_install(port)
    elif uninstall:
        _remote_uninstall()
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


def _remote_install(port: int) -> None:
    import subprocess
    import sys

    from remote import auth as rauth

    if not rauth.is_setup_done():
        console.print(
            "[bold red]Run setup first:[/] [cyan]python main.py remote --setup[/]"
        )
        return

    python_bin  = sys.executable
    main_script = str(Path(__file__).resolve())
    work_dir    = str(Path(__file__).parent.resolve())
    plist_path  = Path.home() / "Library" / "LaunchAgents" / "com.nanoclaw.remote.plist"
    log_path    = Path.home() / "Library" / "Logs" / "nanoclaw-remote.log"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.nanoclaw.remote</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>{main_script}</string>
    <string>remote</string>
    <string>--port</string>
    <string>{port}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{work_dir}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>{Path.home()}</string>
  </dict>
</dict>
</plist>"""

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # Unload any existing instance before overwriting
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.write_text(plist)

    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[bold red]Failed to load Launch Agent:[/]\n{result.stderr or result.stdout}")
        return

    console.print(
        Panel.fit(
            f"[bold green]Auto-start installed![/]\n\n"
            f"Nanoclaw Remote now starts automatically every time\n"
            f"this Mac logs in — no action needed from you.\n\n"
            f"Log file: [dim]{log_path}[/]\n"
            f"To remove: [cyan]python main.py remote --uninstall[/]",
        )
    )


def _remote_uninstall() -> None:
    import subprocess

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.nanoclaw.remote.plist"
    if not plist_path.exists():
        console.print("[yellow]Auto-start is not installed.[/]")
        return
    subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True)
    plist_path.unlink()
    console.print("[bold green]Auto-start removed.[/] Nanoclaw Remote will no longer start on login.")


def _save_url_everywhere(url: str) -> None:
    """Write the public URL to iCloud Drive and Desktop so it's always findable."""
    content = (
        f"Nanoclaw Remote — your access link\n"
        f"===================================\n\n"
        f"  {url}\n\n"
        f"Open this link in any browser (phone, iPad, laptop).\n"
        f"Valid as long as your Mac mini stays on.\n"
    )
    saved = []

    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.exists():
        try:
            target = icloud / "Nanoclaw Remote URL.txt"
            target.write_text(content)
            saved.append("iCloud Drive → Nanoclaw Remote URL.txt")
        except Exception:
            pass

    try:
        desktop = Path.home() / "Desktop" / "nanoclaw-remote-url.txt"
        desktop.write_text(content)
        saved.append("Desktop → nanoclaw-remote-url.txt")
    except Exception:
        pass

    if saved:
        console.print(f"[dim]URL also saved to: {' | '.join(saved)}[/]")


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
                        f"[dim]Saving URL + sending push notification…[/]"
                    )
                    _save_url_everywhere(public_url)
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
