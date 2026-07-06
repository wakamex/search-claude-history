"""Console entrypoint that avoids shadowing by similarly named scripts."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def main():
    cli_path = Path(__file__).with_name("search_claude_history") / "cli.py"
    spec = spec_from_file_location("_search_claude_history_cli", cli_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load CLI module from {cli_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main()
