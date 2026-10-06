import os
import re
import sys

import pytest
from conftest import run_cli
from mock_server import load_openapi

import tourclaim
from tourclaim.cli import COMMANDS
from tourclaim.errors import CONFLICT_CODES
from tourclaim.fields import INTAKE_FIELDS, MEDICAL_FIELDS
from tourclaim.models import ATTACHMENT_CONTENT_TYPES, DOC_TYPES, EMAIL_PROVIDERS, INTAKE_STATES, OTHER_INSURANCE, REASON_CATEGORIES, SCOPES

PYTHON_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OPENAPI = load_openapi()
needs_schema = pytest.mark.skipif(OPENAPI is None, reason="openapi/connectors-v1.json is not in this checkout")


def read(path):
    with open(os.path.join(PYTHON_DIR, path), encoding="utf-8") as handle:
        return handle.read()


# ---- package ----


def test_pyproject_has_no_runtime_dependencies_and_full_metadata():
    text = read("pyproject.toml")
    if sys.version_info >= (3, 11):
        import tomllib

        project = tomllib.loads(text)["project"]
        assert project["name"] == "tourclaim"
        assert project["dependencies"] == []
        assert project["requires-python"] == ">=3.9"
        assert project["license"] == "MIT"
        assert project["scripts"] == {"tourclaim": "tourclaim.cli:main"}
        assert project["urls"]["Homepage"] == "https://github.com/tourclaim/tourclaim-cli"
        assert {"Issues", "Changelog"} <= set(project["urls"])
        for keyword in ("travel insurance", "trip cancellation", "claims", "credit card benefits", "ai agents", "cli", "sdk"):
            assert keyword in project["keywords"], keyword
        assert project["optional-dependencies"]["dev"] == ["pytest>=7"]
    else:
        assert re.search(r"^dependencies = \[\]$", text, re.M)
    assert 'build-backend = "hatchling.build"' in text
    assert 'path = "src/tourclaim/__init__.py"' in text


def test_version_is_in_the_changelog():
    assert re.search(rf"^## \[{re.escape(tourclaim.__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", read("CHANGELOG.md"), re.M)


def test_uses_no_em_dashes_in_docs_or_source():
    files = ["README.md", "CHANGELOG.md", "RELEASING.md", "pyproject.toml"]
    for directory in ("src/tourclaim", "tests"):
        for name in os.listdir(os.path.join(PYTHON_DIR, directory)):
            if name.endswith(".py"):
                files.append(f"{directory}/{name}")
    for path in files:
        assert chr(0x2014) not in read(path), f"{path} contains an em dash"


@pytest.mark.skipif(not hasattr(sys, "stdlib_module_names"), reason="needs Python 3.10+")
def test_the_package_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names) | {"__future__", "tourclaim"}
    for name in os.listdir(os.path.join(PYTHON_DIR, "src", "tourclaim")):
        if name == "mcp.py":
            continue  # Optional adapter is imported only by tourclaim mcp.
        if name.endswith(".py"):
            for match in re.finditer(r"^\s*(?:from|import) ([A-Za-z_]\w*)", read(f"src/tourclaim/{name}"), re.M):
                if name == "cli.py" and match.group(1) == "mcp":
                    continue  # Lazy optional-dependency check in run_mcp.
                assert match.group(1) in allowed, f"{name} imports {match.group(1)}"


# ---- schema ----


@needs_schema
def test_intake_fields_match_the_schema():
    schemas = OPENAPI["components"]["schemas"]
    assert list(INTAKE_FIELDS) == list(schemas["IntakeFields-Input"]["properties"])
    assert list(MEDICAL_FIELDS) == list(schemas["MedicalAnswers"]["properties"])


@needs_schema
def test_enums_match_the_schema():
    schemas = OPENAPI["components"]["schemas"]
    assert list(REASON_CATEGORIES) == schemas["ReasonCategory"]["enum"]
    other = next(s for s in schemas["IntakeFields-Input"]["properties"]["other_insurance"]["anyOf"] if "enum" in s)
    assert list(OTHER_INSURANCE) == other["enum"]
    assert list(INTAKE_STATES) == schemas["IntakeResponse"]["properties"]["state"]["enum"]
    assert list(DOC_TYPES) == schemas["AttachmentEvidence"]["properties"]["doc_type"]["enum"]
    assert list(ATTACHMENT_CONTENT_TYPES) == schemas["AttachmentEvidence"]["properties"]["content_type"]["enum"]
    assert sorted(EMAIL_PROVIDERS) == sorted(schemas["EmailEvidence"]["properties"]["provider"]["enum"])
    assert list(SCOPES) == schemas["KeyResponse"]["properties"]["scopes"]["items"]["enum"]
    assert sorted(schemas["KeyResponse"]["properties"]) == ["account_email", "expires_at", "id", "scopes"]


@needs_schema
def test_every_operation_has_a_client_method():
    methods = {
        "search_credit_cards": "search_cards",
        "start_travel_claim": "start_intake",
        "list_claim_drafts": "list_drafts",
        "get_travel_claim_intake": "get_intake",
        "update_travel_claim_intake": "update_intake",
        "delete_travel_claim_draft": "delete_draft",
        "import_selected_email": "add_email",
        "import_claim_attachment": "add_attachment",
        "submit_authorized_travel_claim": "submit",
        "list_my_claims": "list_claims",
        "get_my_claim_status": "get_claim",
        "get_connection": "get_connection",
        "disconnect": "disconnect",
        "request_data_deletion": "request_data_deletion",
    }
    operations = {op["operationId"] for path in OPENAPI["paths"].values() for op in path.values()}
    assert operations == set(methods), "a new API operation needs a client method"
    for operation, method in methods.items():
        assert callable(getattr(tourclaim.Client, method)), operation
        assert operation in (getattr(tourclaim.Client, method).__doc__ or ""), f"{method} docstring names {operation}"


@needs_schema
def test_conflict_codes_are_the_ones_the_api_names():
    for code in CONFLICT_CODES:
        assert code in OPENAPI["info"]["description"], code


# ---- help ----


def test_every_command_has_help_exiting_0_without_network(home):
    for cmd in COMMANDS:
        r = run_cli([*cmd.path, "--help"], home=home, api_url="http://127.0.0.1:9")
        assert r.code == 0, cmd.path
        assert r.stdout.startswith(f"Usage: tourclaim {' '.join(cmd.path)}")
    for argv in ([], ["--help"], ["help"], ["help", "intake", "attach"], ["intake", "--help"], ["cards"], ["help", "whoami"], ["--json", "help", "status"]):
        r = run_cli(argv, home=home)
        assert r.code == 0, argv
        assert "Usage: tourclaim" in r.stdout
    assert run_cli(["--version"], home=home).stdout == f"{tourclaim.__version__}\n"
    assert run_cli(["--version", "--json"], home=home).json() == {"name": "tourclaim", "version": tourclaim.__version__}
    assert run_cli(["status", "--version"], home=home).stdout == f"{tourclaim.__version__}\n"


def test_unknown_commands_exit_2_as_json_in_json_mode(home):
    r = run_cli(["frobnicate", "--json"], home=home)
    assert r.code == 2
    assert r.json_error()["code"] == "usage_error"
    assert r.stdout == ""
    assert run_cli(["help", "frobnicate"], home=home).code == 2
    assert run_cli(["intake", "show", "a", "b"], home=home).code == 2


def test_options_may_come_before_the_command(home):
    r = run_cli(["--api-url", "http://127.0.0.1:9", "--json", "intake", "--help"], home=home)
    assert r.code == 0 and "Usage: tourclaim intake" in r.stdout


def test_mcp_release_metadata_and_examples_match_package_version():
    import json
    from pathlib import Path
    root = Path(PYTHON_DIR).parent
    if not (root / "server.json").exists():
        pytest.skip("release files are in the repository, not the Python sdist")
    manifest = json.loads((root / "server.json").read_text())
    assert manifest["name"] == "io.github.tourclaim/tourclaim"
    assert manifest["version"] == tourclaim.__version__
    assert f"mcp-name: {manifest['name']}" in read("README.md")
    package = manifest["packages"][0]
    assert package["identifier"] == "tourclaim"
    assert package["version"] == tourclaim.__version__
    assert package["transport"] == {"type": "stdio"}
    assert package["runtimeArguments"][-1]["value"] == f"tourclaim[mcp]=={tourclaim.__version__}"
    for file in (root / "examples").glob("*mcp*.json"):
        example = json.loads(file.read_text())
        server = example.get("servers", example.get("mcpServers"))["tourclaim"]
        assert server["command"] == "uvx"
        assert server["args"] == ["--python", "3.12", "--from", f"tourclaim[mcp]=={tourclaim.__version__}", "tourclaim", "mcp"]
    assert json.loads((root / "package.json").read_text())["version"] == tourclaim.__version__
