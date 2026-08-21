from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich import print as rprint
from rich.table import Table

app = typer.Typer(no_args_is_help=True, help="rca-model-lab 작업 진입점")
scen = typer.Typer(no_args_is_help=True, help="시나리오(ground truth) 조회")
synth = typer.Typer(no_args_is_help=True, help="교사 모델로 SFT 데이터 합성")
data = typer.Typer(no_args_is_help=True, help="synth → processed 빌드")
app.add_typer(scen, name="scenarios")
app.add_typer(synth, name="synth")
app.add_typer(data, name="data")


@scen.command("list")
def scen_list() -> None:
    from rca_lab.scenarios import load_scenarios

    t = Table("key", "title", "difficulty", "root_cause")
    for s in load_scenarios():
        t.add_row(s.key, s.title, str(s.difficulty or "-"), s.root_cause[:70])
    rprint(t)


@scen.command("show")
def scen_show(key: str) -> None:
    from rca_lab.scenarios import load_scenarios

    for s in load_scenarios():
        if s.key == key:
            rprint(s.model_dump(exclude={"script_path"}))
            return
    raise typer.BadParameter(f"unknown scenario {key}")


@synth.command("run")
def synth_run(
    teacher: Annotated[str, typer.Option(help="claude | codex")] = "claude",
    config: Annotated[Path, typer.Option()] = Path("configs/synth/default.yaml"),
    out: Annotated[Path | None, typer.Option()] = None,
) -> None:
    from rca_lab.synth.generate import run

    run(config, teacher, out)


@synth.command("smoke")
def synth_smoke(teacher: str = "claude") -> None:
    """교사 CLI 가 살아있는지 1회 호출로 확인."""
    from rca_lab.synth import get_teacher

    rprint(get_teacher(teacher).complete("Reply with exactly: OK"))


@data.command("build")
def data_build(name: str = "sft_v0", eval_ratio: float = 0.2) -> None:
    from rca_lab.data.build import build

    build(name, eval_ratio)


if __name__ == "__main__":
    app()
