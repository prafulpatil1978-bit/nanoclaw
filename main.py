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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[bold red]Error:[/] ANTHROPIC_API_KEY is not set.\n"
            "Copy [cyan].env.example[/] to [cyan].env[/] and add your key."
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
    type=click.Choice(["auto", "parametric", "mesh"]),
    default="auto",
    show_default=True,
    help="auto | parametric (OpenSCAD) | mesh (AI mesh generation)",
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
        "parametric": "Parametric (OpenSCAD)",
        "mesh": f"AI Mesh ({backend_labels[backend]})",
    }[mode]

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

    if result.design.warnings or result.partition.warnings:
        console.print("\n[yellow]Warnings:[/]")
        for w in result.design.warnings + result.partition.warnings:
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
