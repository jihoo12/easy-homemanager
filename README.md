# ehm — easy Home Manager

**English** · [한국어](README.ko.md)

`ehm` is a semi-automatic command-line interface for [Home Manager](https://github.com/nix-community/home-manager). It installs packages, configures program options and adopts dotfiles without requiring you to write Nix by hand. It can also analyze an existing configuration and suggest improvements.

Everything `ehm` produces is a plain `home.nix`. Edits are surgical: comments and formatting are preserved and only the affected lines change, so you can stop using `ehm` at any time and keep maintaining the files yourself.

## Getting started

```bash
nix run path:. -- status     # run without installing (from this directory)
nix run path:.               # open the full-screen interface
```

To install it permanently, add this repository as a flake input and put `inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default` in `home.packages`.

`ehm` enables the `nix-command` and `flakes` experimental features for its own invocations, so no extra Nix configuration is needed. The command-line interface depends only on the Python standard library (3.11+); the full-screen interface additionally needs [Textual](https://textual.textualize.io/), which the Nix package includes (`pip install 'easy-homemanager[tui]'` otherwise).

## Full-screen interface

Running `ehm` without arguments in a terminal opens a full-screen interface (`ehm tui`). Without Textual, or when not attached to a terminal, it falls back to a plain text menu (`ehm menu`).

| Tab | What you can do |
|---|---|
| **1 Overview** | Detected setup, managed counts, pending dotfile operations, git state |
| **2 Suggestions** | Analyze the configuration; tick suggestions with Space and stage them |
| **3 Packages** | Search nixpkgs and stage installs with Enter; `x` removes a package or disables a module |
| **4 Programs** | Browse every Home Manager program module and its options with descriptions; Enter edits a value |
| **5 Dotfiles** | Adopt a file (symlink or read-only copy); `x` stops managing one |

Changes are only **staged** while you work. Press `s` to review the combined diff and save it, or discard everything. After saving, the configuration is evaluated and you are offered a revert if it fails.

| Key | Action |
|---|---|
| `s` | Review and save staged changes |
| `a` / `c` / `u` | Apply / preview build / update flake inputs |
| `g` / `b` | List generations / roll back |
| `e` | Open the home file in `$EDITOR` |
| `r` | Reload from disk |
| `q` | Quit (asks if changes are staged) |

Operations that need a real terminal, such as `sudo nixos-rebuild` and build logs, temporarily hand the terminal back and return to the interface afterwards.

## Usage

```bash
ehm                          # full-screen interface (or text menu without Textual)
ehm menu                     # plain text menu
ehm suggest                  # analyze the configuration and apply selected suggestions
ehm setup                    # pick recommended programs to enable

ehm add ripgrep bat          # install packages (verifies they exist in nixpkgs)
ehm remove bat
ehm list
ehm search 'json viewer'

ehm configure git            # browse options with descriptions and set them
ehm enable bat               # enable programs.bat
ehm set programs.git.settings.init.defaultBranch main
ehm get programs.git.settings.user.name

ehm dot add ~/.config/ghostty   # move into the repository and start managing
ehm dot list
ehm dot edit ghostty
ehm dot rm ghostty              # restore as a regular file and stop managing

ehm check                    # build and preview changes (no sudo)
ehm apply                    # apply
ehm update                   # update flake inputs and apply
ehm generations              # list generations
ehm rollback                 # switch to the previous generation
ehm gc                       # delete old generations
```

Commands that edit the configuration accept `--apply` to apply immediately after saving, and `-y` to skip confirmations.

## Safety

- **Preview first.** Every change is shown as a diff and requires confirmation.
- **Verify on save.** The result is parsed and evaluated with Nix right away; on failure you are offered a revert (automatic with `-y`).
- **Build before switching.** The Home Manager generation is built without sudo first, catching errors early and showing which packages and files will change.
- **Never delete files.** Conflicting files are moved to `~/.local/state/ehm/backups/` and restored if the switch fails. Conflicts with files you did not adopt are moved only with your permission.
- **Guard against secrets.** `dot add` refuses files that look like private keys, tokens or passwords, and directories such as `~/.ssh`. Detection is pattern-based and can miss things, so review your files before pushing to a public repository.
- **Handle flake pitfalls.** Flakes only see files tracked by git, so newly adopted dotfiles are `git add`ed automatically. Commits and pushes happen only when requested.

## Dotfile modes

| | Default (symlink) | `--copy` |
|---|---|---|
| Edits take effect | Immediately | After `ehm apply` |
| Application can write to the file | Yes | No (read-only) |
| Repository moved | Needs re-setup | Unaffected |

## Supported setups

The setup is detected automatically.

| Setup | Apply command | Preview & option browsing |
|---|---|---|
| NixOS module + flake | `sudo nixos-rebuild switch --flake` | Yes |
| Standalone + flake | `home-manager switch --flake` | Yes |
| NixOS module + channels | `sudo nixos-rebuild switch` | No |
| Standalone + channels | `home-manager switch` | No |

When Home Manager runs as a NixOS module, `apply` and `rollback` affect the whole system. Without an existing configuration, `ehm init` creates a new standalone flake.

Structures that cannot be expanded statically, such as `lib.mkMerge`, are never rewritten by guesswork; `ehm` tells you to edit them manually instead.

## Configuration

`~/.config/ehm/config.toml` (every key is optional; run `ehm config` for a summary)

```toml
config_dir = "/etc/nixos"
home_file = "home.nix"
host = "nixos"
dotfiles_dir = "dotfiles"
extra_rebuild_args = ["--show-trace"]
auto_commit = false
auto_push = false
```

## Development

```bash
nix develop                  # Python with Textual, plus ruff; sets PYTHONPATH=src
python -m unittest discover -s tests
python -m ehm status
ruff check src tests
```

Without Textual installed, the TUI tests are skipped.

| Module | Responsibility |
|---|---|
| `nixfile` | Format-preserving tokenizer and editor for Nix modules |
| `context` | Detecting the setup and running `nix` |
| `workspace` | Editing sessions: staging, diffing, validating and writing |
| `options` | Reading option metadata and interactive value prompts |
| `dotfiles` | Adopting, releasing and secret detection |
| `suggest` | Configuration analysis |
| `apply` | Building, previewing, conflict handling and switching |
| `cli` | Argument parsing, commands and the text menu |
| `tui` | Full-screen interface (Textual) |
| `ui` | Terminal output, prompts and output redirection for the TUI |

## License

[Apache-2.0](LICENSE)
