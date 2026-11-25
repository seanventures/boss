#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const PIN_SHA: &str = "05ae1b0fdd5ab412d24248acc76c5d9f6f275440";
const LICENSE_SHA256: &str = "d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc";
const NOTICE_SHA256: &str = "9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915";

fn metatron_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("metatron root")
        .to_path_buf()
}

fn collect_rs_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let entries = fs::read_dir(dir).expect("read_dir");
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_rs_files(&path, out);
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            out.push(path);
        }
    }
}

#[test]
fn vendored_codex_files_have_license_headers() {
    let vendored_dir = metatron_root().join("third_party/codex");
    assert!(
        vendored_dir.join("LICENSE").is_file(),
        "missing vendored LICENSE"
    );
    assert!(
        vendored_dir.join("NOTICE").is_file(),
        "missing vendored NOTICE"
    );

    let mut rs_files = Vec::new();
    collect_rs_files(&vendored_dir, &mut rs_files);
    assert!(
        !rs_files.is_empty(),
        "expected vendored .rs files under third_party/codex"
    );

    for path in rs_files {
        let text = fs::read_to_string(&path).expect("read vendored rs file");
        assert!(
            text.contains("Copyright 2025 OpenAI"),
            "missing copyright header in {}",
            path.display()
        );
        assert!(
            text.contains("SPDX-License-Identifier: Apache-2.0"),
            "missing SPDX header in {}",
            path.display()
        );
        assert!(
            text.contains(PIN_SHA),
            "missing upstream commit pin in {}",
            path.display()
        );
    }
}

#[test]
fn third_party_notices_record_codex_pin_crates_and_ratatui() {
    let metatron = metatron_root();
    let repo_root = metatron.parent().expect("repo root");

    let notices_md = fs::read_to_string(repo_root.join("THIRD-PARTY-NOTICES.md"))
        .expect("THIRD-PARTY-NOTICES.md");
    for expected in [
        PIN_SHA,
        "codex-rs/utils/head-tail-buffer",
        "codex-rs/utils/pty",
        "codex-rs/keyring-store",
        "Apache-2.0",
        "Florian Dehau",
        "The Ratatui Developers",
        LICENSE_SHA256,
        NOTICE_SHA256,
    ] {
        assert!(
            notices_md.contains(expected),
            "THIRD-PARTY-NOTICES.md missing {expected}"
        );
    }

    let retained_license = fs::read(repo_root.join("third_party/notices/codex-05ae1b0f-LICENSE"))
        .expect("codex-05ae1b0f-LICENSE");
    let vendored_license =
        fs::read(metatron.join("third_party/codex/LICENSE")).expect("vendored LICENSE");
    assert_eq!(retained_license.len(), 10_926);
    assert_eq!(retained_license, vendored_license);

    let retained_notice = fs::read(repo_root.join("third_party/notices/codex-05ae1b0f-NOTICE"))
        .expect("codex-05ae1b0f-NOTICE");
    let vendored_notice =
        fs::read(metatron.join("third_party/codex/NOTICE")).expect("vendored NOTICE");
    assert_eq!(retained_notice.len(), 242);
    assert_eq!(retained_notice, vendored_notice);
}

#[test]
fn metatron_binary_prints_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_metatron"))
        .arg("--version")
        .output()
        .expect("spawn metatron --version");
    assert!(
        output.status.success(),
        "metatron --version failed: {:?}",
        output.status
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.starts_with("metatron 0.1.0"),
        "unexpected --version output: {stdout:?}"
    );
}
