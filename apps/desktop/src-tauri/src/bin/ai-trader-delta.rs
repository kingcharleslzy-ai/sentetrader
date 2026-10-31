//! Release-side BSDIFF40 tool. Uses the same patch implementation as the client.
use std::{env, fs, io::Cursor};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    if args.len() != 4 || !matches!(args[0].as_str(), "diff" | "patch") {
        return Err("usage: ai-trader-delta diff|patch BASE INPUT OUTPUT".into());
    }
    let base = fs::read(&args[1])?;
    let input = fs::read(&args[2])?;
    let mut output = Vec::new();
    if args[0] == "diff" {
        qbsdiff::Bsdiff::new(&base, &input).compare(Cursor::new(&mut output))?;
    } else {
        qbsdiff::Bspatch::new(&input)?.apply(&base, Cursor::new(&mut output))?;
    }
    fs::write(&args[3], output)?;
    Ok(())
}
