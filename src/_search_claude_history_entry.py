"""Console entrypoint that avoids shadowing by similarly named scripts."""

from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path


def main():
    cli_path = Path(__file__).with_name("search_claude_history") / "cli.py"
    if not cli_path.exists():
        # Editable installs leave the package in src/ instead of beside this module.
        package = find_spec("search_claude_history")
        if package is None or package.origin is None:
            raise RuntimeError("could not find the search_claude_history package")
        cli_path = Path(package.origin).with_name("cli.py")
    spec = spec_from_file_location("_search_claude_history_cli", cli_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load CLI module from {cli_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main()
