# Generated HTML report previews

BOSS supports CLI tools whose browser-visible output is a self-contained HTML
report. The CLI does not have to become a web application. Declare the artifact
in `boss.preview.report` in the app directory:

```json
{"path": "report.html", "prepare": "go run . demo-packages"}
```

Use the CLI's actual generation command and commit its demo inputs. Preview
workspaces are fresh clones: output generated during tests is not retained.
`prepare` is optional for an HTML artifact already present in the clone. It runs
under the existing preview execution policy, with a 120-second timeout, and must
succeed. The file must exist afterward. Missing output fails explicitly rather
than showing a directory listing or a placeholder success page.

BOSS serves only that file at the preview root, index.html, and its filename.
Relative assets are not served; embed styles/images in the report. Absolute paths,
parent traversal and symlinks escaping the app directory are rejected. Existing
HTTP services continue to use `boss.preview.command` unchanged. A report
declaration takes precedence in the same app directory.

The declaration is used by preview startup and build-time HTTP proof. Passing
that proof does not replace functional acceptance tests or approve a merge.
Existing CLI builds with desktop openers need a BOSS follow-up that adds this
declaration and demo inputs; recreating the same old build cannot generate them.
