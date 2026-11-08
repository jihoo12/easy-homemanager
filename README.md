# ehm — easy Home Manager

**English** · [한국어](README.ko.md)

`ehm` is a semi-automatic command-line interface for [Home Manager](https://github.com/nix-community/home-manager). It installs packages, configures program options and adopts dotfiles without requiring you to write Nix by hand. It can also analyze an existing configuration and suggest improvements.

Everything `ehm` produces is a plain `home.nix`. Edits are surgical: comments and formatting are preserved and only the affected lines change, so you can stop using `ehm` at any time and keep maintaining the files yourself.

## Getting started

```bash
nix run path:. -- status     # run without installing (from this directory)
nix run path:.               # open the full-screen interface
```

To install it permanently, run `ehm self-install` (also offered by `ehm suggest`). It adds this repository as a flake input (following your `nixpkgs`) and puts `inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default` in `home.packages`, after showing the diff. It requires that Home Manager modules receive the flake's `inputs` (`extraSpecialArgs = { inherit inputs; }`); if they do not, it tells you what to add. Set `ehm_flake_url` in the settings to install from a fork or a private repository (e.g. `git+ssh://...`).

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
| **6 Generations** | A plain-language summary of how a generation differs from now, and its configuration side by side with the current one (`t` for a unified diff); Enter switches to it, `w` stages its configuration |
| **7 Inputs** | Flake inputs with their source, pinned revision, nixpkgs relationship and where they are used; `n` adds a flake (pick its package and/or Home Manager module), `x` removes one, `U` updates one |

Changes are only **staged** while you work. Press `s` to review the combined diff and save it, or discard everything. After saving, the configuration is evaluated and you are offered a revert if it fails.

| Key | Action |
|---|---|
| `s` | Review and save staged changes |
| `a` / `c` / `u` | Apply / preview build / update flake inputs |
| `m` | Commit the configuration repository and push it |
| `g` / `b` | Generations tab / roll back to the previous generation |
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
ehm generations 31 --diff    # what generation 31's configuration changes compared to now
ehm generations 31 --restore # stage generation 31's configuration files
ehm rollback [31]            # switch to the previous generation (or to 31)
ehm self-install             # install ehm through your flake
ehm inputs                   # flake inputs: source, pinned revision, where they are used
ehm inputs add https://github.com/nix-community/nixvim   # inspect, then add its package/module
ehm inputs remove nixvim     # remove the input and its package/module entries
ehm update nixvim            # update a single input
ehm commit [-m MSG] [--push] # commit tracked changes, dotfiles/ and new .nix files
ehm push
ehm gc                       # delete old generations
```

Commands that edit the configuration accept `--apply` to apply immediately after saving, and `-y` to skip confirmations.

## Flake inputs

Packages and Home Manager modules that are not in nixpkgs come from other flakes. `ehm inputs add <url>` accepts a flake reference or a GitHub/GitLab URL, inspects what the flake provides for your system, and stages the input in `flake.nix` together with `inputs.<name>.packages.${system}.<package>` in `home.packages` and/or `inputs.<name>.<homeModules>.<module>` in `imports`. By default the new input follows your `nixpkgs`, which avoids a second download; flakes that ship a binary cache built against their own nixpkgs may then build from source, so `--no-follow` keeps their own.

`ehm inputs remove <name>` removes the input and those entries. If the input is used anywhere ehm does not rewrite (another option, `outputs`, another `.nix` file, or another input following it), it lists those places and changes nothing.

## Generations

`ehm rollback` switches the system (NixOS module) or Home Manager to an earlier generation, but leaves your configuration files alone, so the next apply builds the current files again. To go back for good, restore the generation's configuration as well (`ehm generations N --restore`, or `w` in the TUI) and save.

A generation does not contain the Nix source it was built from. `ehm apply` therefore records the configuration of every generation it builds; for older generations, ehm shows the last git commit made before the generation and labels it as an approximation.

## Safety

- **Preview first.** Every change is shown as a diff and requires confirmation.
- **Verify on save.** The result is parsed and evaluated with Nix right away; on failure you are offered a revert (automatic with `-y`).
- **Build before switching.** The Home Manager generation is built without sudo first, catching errors early and showing which packages and files will change.
- **Never delete files.** Conflicting files are moved to `~/.local/state/ehm/backups/` and restored if the switch fails. Conflicts with files you did not adopt are moved only with your permission.
- **Guard against secrets.** `dot add` refuses files that look like private keys, tokens or passwords, and directories such as `~/.ssh`. Detection is pattern-based and can miss things, so review your files before pushing to a public repository.
- **Handle flake pitfalls.** Flakes only see files tracked by git, so newly adopted dotfiles are `git add`ed automatically. Commits and pushes happen only when requested (`ehm commit`, `m` in the TUI); they include tracked files, `dotfiles/` and new `.nix` files, and leave other untracked files out in case they hold secrets.

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
ehm_flake_url = "github:jihoo12/easy-homemanager"
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
| `generations` | Listing generations, recovering their configuration, rollback |
| `inputs` | Flake inputs: listing, inspecting, adding, removing |
| `install` | Installing ehm itself through the user's flake |
| `vcs` | Committing and pushing the configuration repository |
| `apply` | Building, previewing, conflict handling and switching |
| `cli` | Argument parsing, commands and the text menu |
| `tui` | Full-screen interface (Textual) |
| `ui` | Terminal output, prompts and output redirection for the TUI |

## License

[Apache-2.0](LICENSE)
