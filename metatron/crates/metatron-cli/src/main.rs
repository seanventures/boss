//! Governed operator CLI entry point for BOSS.

use clap::Parser;

#[derive(Debug, Parser)]
#[command(name = "metatron", version, about = "BOSS governed operator CLI")]
struct Cli {}

fn main() {
    let _cli = Cli::parse();
}
