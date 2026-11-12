// SPDX-License-Identifier: GPL-3.0-or-later
//! Compiling the game's shaders for this GPU at Set up, so a place seen for the first time does not stutter (or show objects a moment
//! late) while the driver compiles its shaders in play.
//!
//! Set up's extract writes the game's graphics programs to `PROGRAMS_REL` (tools/shaders/ampere/extract_corpus.py --programs). The
//! game's own `--prepare-shaders --if-needed` (game-build/src/pkp_main.cpp; gpl_prepare in runtime/src/gfx/nvn/nvn_gpl.cpp) compiles
//! the vertex and fragment parts of every program into the pipeline cache (`[nvn] pipeline_cache`) and writes
//! shader_cache/prepare.state with the GPU, driver and program count it ran for. While that state is current the game ends at once
//! ("prepare: ready"), so Set up runs it every time and only the first run, or the first after a driver update, takes long.
//! A speed-up, never a requirement: the game still compiles whatever is missing while it runs, so a failure is only a message.

use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

use crate::shaders::{run_tool, LogFn};

/// The program list in the game folder: next to the shader cache's `spirv` folder, where the runtime looks for it.
pub const PROGRAMS_REL: &str = "shader_cache/programs.bin";

pub fn programs_file(game_dir: &Path) -> PathBuf {
    game_dir.join(PROGRAMS_REL)
}

/// How a run ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    /// prepared before, for this GPU and driver
    Ready,
    /// compiled now; the game's summary ("programs=... prepared=... seconds=...")
    Done(String),
    /// no program list: a cache set up before the extract wrote one; the next Set up that extracts writes it
    NoList,
    Cancelled,
}

/// One stdout line of `--prepare-shaders` (prepare_shaders_main in game-build/src/pkp_main.cpp).
#[derive(Debug, PartialEq, Eq)]
pub enum Line {
    Progress(u64, u64),
    Ready,
    Done(String),
    Error(String),
}

pub fn parse_line(l: &str) -> Option<Line> {
    let rest = l.trim().strip_prefix("prepare: ")?;
    if rest == "ready" {
        return Some(Line::Ready);
    }
    if let Some(s) = rest.strip_prefix("done") {
        return Some(Line::Done(s.trim().to_string()));
    }
    if let Some(e) = rest.strip_prefix("error ") {
        return Some(Line::Error(e.trim().to_string()));
    }
    let mut it = rest.split_whitespace().map(|x| x.parse::<u64>().ok());
    match (it.next().flatten(), it.next().flatten(), it.next()) {
        (Some(done), Some(total), None) => Some(Line::Progress(done, total)),
        _ => None,
    }
}

/// One preparation run of the game in `game_dir`; `progress` gets (programs done, programs). Err: the game's own reason
/// ("prepare: error ..."), or how it ended.
pub fn run(game_dir: &Path, cancel: &AtomicBool, idle: bool, log: Option<&LogFn>, mut progress: impl FnMut(u64, u64)) -> Result<Outcome, String> {
    if !programs_file(game_dir).is_file() {
        return Ok(Outcome::NoList);
    }
    let mut cmd = crate::host::command(crate::host::game_exe(game_dir));
    cmd.current_dir(game_dir).args(["--prepare-shaders", "--if-needed"]);
    let (mut end, mut error) = (None, None);
    let finished = run_tool(cmd, cancel, idle, log, None, |l| match parse_line(l) {
        Some(Line::Progress(d, t)) => progress(d, t),
        Some(Line::Ready) => end = Some(Outcome::Ready),
        Some(Line::Done(s)) => end = Some(Outcome::Done(s)),
        Some(Line::Error(e)) => error = Some(e),
        None => {}
    })
    .map_err(|e| error.clone().unwrap_or(e))?;
    if !finished {
        return Ok(Outcome::Cancelled);
    }
    if let Some(e) = error {
        return Err(e);
    }
    Ok(end.unwrap_or(Outcome::Done(String::new())))
}

/// `run` as a Set up phase ("prepare"): its progress and its end as messages. Ok(false): paused. A failure is a message only.
pub fn set_up_phase(game_dir: &Path, cancel: &AtomicBool, idle: bool, log: Option<&LogFn>, progress: &mut dyn FnMut(&str, u64, u64, &str)) -> bool {
    if !crate::host::prepares_shaders() {
        return true;
    }
    progress("prepare", 0, 0, "Preparing the shaders for your graphics card...");
    let r = run(game_dir, cancel, idle, log, |d, t| progress("prepare", d, t, &format!("Preparing the shaders for your graphics card: {} of {}", d, t)));
    match r {
        Ok(Outcome::Cancelled) => return false,
        Ok(Outcome::Ready) => progress("prepare", 1, 1, "The shaders are prepared for this graphics card already."),
        Ok(Outcome::Done(_)) => progress("prepare", 1, 1, "The shaders are prepared for your graphics card."),
        Ok(Outcome::NoList) => progress("prepare", 1, 1, "No shader list to prepare from yet: the game compiles its shaders while it runs."),
        Err(e) => progress("prepare", 1, 1, &format!("The shaders could not be prepared ahead: {}. The game compiles them while it runs, as before.", e)),
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepare_lines() {
        assert_eq!(parse_line("prepare: 192 98304"), Some(Line::Progress(192, 98304)));
        assert_eq!(parse_line("prepare: ready"), Some(Line::Ready));
        assert_eq!(
            parse_line("prepare: done programs=10 prepared=9 missing=1 failed=0 parts=19 fast=0 seconds=2.5"),
            Some(Line::Done("programs=10 prepared=9 missing=1 failed=0 parts=19 fast=0 seconds=2.5".into()))
        );
        assert_eq!(parse_line("prepare: error no shader cache is open"), Some(Line::Error("no shader cache is open".into())));
        assert_eq!(parse_line("prepare: 1 2 3"), None);
        assert_eq!(parse_line("prepare: soon"), None);
        assert_eq!(parse_line("nvn: shader pack: 12 entries"), None);
    }

    #[test]
    fn no_program_list_is_no_run() {
        let dir = std::env::temp_dir().join(format!("pkp-prepare-test-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let r = run(&dir, &AtomicBool::new(false), false, None, |_, _| {});
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(r, Ok(Outcome::NoList));
    }
}
