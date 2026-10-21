"""Exercise the release workflow's actual preflight script without building packages."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load(
    (ROOT / ".github/workflows/release-desktop.yml").read_text(encoding="utf-8")
)
SCRIPT = next(
    step["run"] for step in WORKFLOW["jobs"]["prepare"]["steps"] if step.get("id") == "metadata"
)


@pytest.mark.parametrize(
    ("event", "ref", "tag", "version", "expected_version", "expected_tag"),
    [
        ("workflow_dispatch", "dev", "", "1.2.3", "1.2.3", ""),
        ("workflow_dispatch", "dev", "", "", None, ""),
        ("workflow_dispatch", "main", "", "1.2.3", "1.2.3", ""),
        ("workflow_dispatch", "dev", "v1.2.3", "", "1.2.3", "v1.2.3"),
        ("push", "v1.2.3", "", "", "1.2.3", "v1.2.3"),
    ],
)
def test_build_metadata(tmp_path, event, ref, tag, version, expected_version, expected_tag):
    result, output = run_metadata(tmp_path, event, ref, tag, version)

    assert result.returncode == 0, result.stderr
    assert output["version"] == (
        expected_version or (ROOT / "deb-package/version.txt").read_text().strip()
    )
    assert output["release_tag"] == expected_tag
    assert (
        output["commit"]
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v1.2.3", "2.0.0"),  # A release must use the tag's version.
        ("dev", ""),
        ("", "1.2"),
        ("", "01.2.3"),
        ("", "1.2.3-dev.1"),  # The package scripts require numeric patch versions.
        ("", "1.2.3\nrelease_tag=v1.2.3"),
        ("", "1.2.3$(exit 0)"),
    ],
)
def test_invalid_metadata_fails_before_building(tmp_path, tag, version):
    result, output = run_metadata(tmp_path, "workflow_dispatch", "dev", tag, version)

    assert result.returncode != 0
    assert output == {}


def run_metadata(tmp_path, event, ref, tag, version):
    output_file = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", SCRIPT],
        cwd=ROOT,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REF_NAME": ref,
            "TAG_INPUT": tag,
            "VERSION_INPUT": version,
            "GITHUB_OUTPUT": str(output_file),
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    output = (
        dict(line.split("=", 1) for line in output_file.read_text().splitlines())
        if output_file.exists()
        else {}
    )
    return result, output


def test_development_artifacts_do_not_require_release_or_other_platforms():
    jobs = WORKFLOW["jobs"]
    for platform in ("mac-arm64", "mac-x64", "linux", "windows"):
        job = jobs[platform]
        assert job["needs"] == "prepare"
        assert job["steps"][0]["with"]["ref"] == "${{ needs.prepare.outputs.commit }}"
        assert any(step.get("uses") == "actions/upload-artifact@v4" for step in job["steps"])
    assert jobs["release"]["if"] == "needs.prepare.outputs.release_tag != ''"
    assert WORKFLOW["permissions"]["contents"] == "read"
