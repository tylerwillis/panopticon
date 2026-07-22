# REQ-001: Codex session scrollback

## Overview

Codex normally uses the terminal's alternate screen buffer. In an attached Panopticon
tmux session, that prevents conversation output from entering terminal scrollback and causes
scroll gestures to be interpreted by Codex's prompt editor instead. Codex's `never`
alternate-screen mode renders inline, allowing the enclosing terminal to retain output and
handle scrolling.

## Requirements

### REQ-001.1: Inline Codex output

1. Panopticon-managed Codex task sessions MUST disable Codex's alternate-screen rendering.
