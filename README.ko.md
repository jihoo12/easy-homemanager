# ehm — easy Home Manager

[English](README.md) · **한국어**

Home Manager 설정을 **반자동으로** 관리하는 CLI입니다. `.nix` 문법을 몰라도 패키지를 설치하고, 프로그램 옵션을 고르고, dotfile을 옮길 수 있습니다. 설정이 어수선하면 분석해서 개선안을 제안합니다.

CLI 메시지는 영어로 출력됩니다. 고친 결과는 평범한 `home.nix`로 남습니다. 주석과 서식을 유지하며 필요한 줄만 바꾸므로, 나중에 `ehm` 없이 직접 관리해도 됩니다.

## 시작하기

```bash
nix run path:. -- status     # 설치 없이 실행 (이 폴더에서)
nix run path:.               # 전체 화면 인터페이스 열기
```

계속 쓰려면 `ehm self-install`을 실행하세요(`ehm suggest`도 권합니다). diff를 보여 준 뒤 이 저장소를 flake 입력으로 추가하고(`nixpkgs`는 내 설정을 따름) `home.packages`에 `inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default`를 넣습니다. Home Manager 모듈이 flake의 `inputs`를 받아야 하며(`extraSpecialArgs = { inherit inputs; }`), 그렇지 않으면 무엇을 추가해야 하는지 알려 줍니다. 포크나 비공개 저장소에서 설치하려면 설정의 `ehm_flake_url`을 바꾸세요(예: `git+ssh://...`).

`nix-command`와 `flakes`는 `ehm`이 알아서 켜므로 따로 설정하지 않아도 됩니다. 명령줄 인터페이스의 의존성은 Python 표준 라이브러리뿐입니다(3.11 이상). 전체 화면 인터페이스에는 [Textual](https://textual.textualize.io/)이 필요한데, Nix 패키지에는 포함되어 있습니다(pip이라면 `pip install 'easy-homemanager[tui]'`).

## 전체 화면 인터페이스

터미널에서 인자 없이 `ehm`을 실행하면 전체 화면 인터페이스가 열립니다(`ehm tui`). Textual이 없거나 터미널이 아니면 텍스트 메뉴(`ehm menu`)로 대신합니다.

| 탭 | 할 수 있는 일 |
|---|---|
| **1 Overview** | 감지된 구성, 관리 중인 항목 수, 적용 대기 dotfile, git 상태 |
| **2 Suggestions** | 설정 분석 후 Space로 제안을 골라 스테이징 |
| **3 Packages** | nixpkgs 검색 후 Enter로 설치 스테이징, `x`로 패키지 제거 또는 모듈 끄기 |
| **4 Programs** | 모든 Home Manager 프로그램 모듈과 옵션을 설명과 함께 탐색, Enter로 값 수정 |
| **5 Dotfiles** | 파일 추가(링크 또는 읽기 전용 복사), `x`로 관리 해제 |
| **6 Generations** | 지금과 무엇이 다른지 요약하고, 그 세대의 설정과 현재 설정을 나란히 보여 줌(`t`로 diff 보기), Enter로 그 세대로 전환, `w`로 그 세대의 설정을 스테이징 |

작업하는 동안 바뀐 내용은 **스테이징**만 됩니다. `s`를 누르면 전체 diff를 보고 저장하거나 모두 버릴 수 있습니다. 저장 후에는 설정을 평가하고, 실패하면 되돌릴지 묻습니다.

| 키 | 동작 |
|---|---|
| `s` | 스테이징한 변경 검토 및 저장 |
| `a` / `c` / `u` | 적용 / 빌드 미리보기 / flake 입력 업데이트 |
| `m` | 설정 저장소 커밋 및 push |
| `g` / `b` | Generations 탭 / 이전 세대로 롤백 |
| `e` | `$EDITOR`로 설정 파일 열기 |
| `r` | 디스크에서 다시 읽기 |
| `q` | 종료 (스테이징한 변경이 있으면 확인) |

`sudo nixos-rebuild`나 빌드 로그처럼 실제 터미널이 필요한 작업은 잠시 터미널을 넘겨주고, 끝나면 인터페이스로 돌아옵니다.

## 무엇을 할 수 있나요

```bash
ehm                          # 전체 화면 인터페이스 (Textual이 없으면 텍스트 메뉴)
ehm menu                     # 텍스트 메뉴
ehm suggest                  # 설정을 분석하고 개선안을 골라서 적용
ehm setup                    # 추천 프로그램을 골라 켜기

ehm add ripgrep bat          # 패키지 설치 (nixpkgs 존재 여부 확인)
ehm remove bat
ehm list
ehm search 'json viewer'

ehm configure git            # 옵션을 설명과 함께 보고 골라서 설정
ehm enable bat               # programs.bat 켜기
ehm set programs.git.settings.init.defaultBranch main
ehm get programs.git.settings.user.name

ehm dot add ~/.config/ghostty   # 저장소로 옮기고 관리 시작
ehm dot list
ehm dot edit ghostty
ehm dot rm ghostty              # 일반 파일로 되돌리고 관리 해제

ehm check                    # 빌드하고 변경 내용 미리보기 (sudo 불필요)
ehm apply                    # 적용
ehm update                   # flake 입력을 올리고 적용
ehm generations              # 세대 목록
ehm generations 31 --diff    # 31번 세대의 설정이 지금과 어떻게 다른지
ehm generations 31 --restore # 31번 세대의 설정 파일을 스테이징
ehm rollback [31]            # 이전 세대(또는 31번)로 전환
ehm self-install             # flake를 통해 ehm 설치
ehm commit [-m 메시지] [--push] # 추적 중인 변경, dotfiles/, 새 .nix 파일 커밋
ehm push
ehm gc                       # 오래된 세대 정리
```

저장을 묻는 명령은 `--apply`로 저장 후 바로 적용할 수 있고, `-y`로 확인을 건너뛸 수 있습니다.

## 세대

`ehm rollback`은 시스템(NixOS 모듈) 또는 Home Manager를 이전 세대로 바꾸지만 설정 파일은 그대로 두므로, 다음 적용 때 현재 파일이 다시 빌드됩니다. 완전히 되돌리려면 그 세대의 설정도 복원하세요(`ehm generations N --restore`, TUI에서는 `w`).

세대에는 빌드에 쓰인 Nix 소스가 들어 있지 않습니다. 그래서 `ehm apply`는 빌드하는 세대마다 설정을 기록해 두고, 그보다 오래된 세대는 그 세대 직전의 git 커밋을 보여 주며 근사치라고 표시합니다.

## 안전장치

- **먼저 보여 줍니다.** 모든 수정은 diff를 보여 주고 확인을 받습니다.
- **저장 후 바로 검사합니다.** 문법과 옵션을 Nix로 평가하고, 실패하면 되돌릴 수 있습니다. `-y`로 실행했다면 자동으로 되돌립니다.
- **적용 전에 빌드합니다.** Home Manager 세대를 sudo 없이 먼저 빌드해 설정 오류를 잡고, 바뀌는 패키지와 파일을 보여 줍니다.
- **파일을 지우지 않습니다.** 충돌 파일은 `~/.local/state/ehm/backups/`로 옮기고, 적용에 실패하면 되돌립니다. 저장소로 옮긴 파일이 아닌 충돌은 허락을 받은 뒤에만 옮깁니다.
- **비밀 정보를 막습니다.** 개인 키, 토큰, 비밀번호로 보이는 파일과 `~/.ssh` 같은 폴더는 `dot add`를 거부합니다. 검사는 패턴 기반이라 놓칠 수 있으니, 공개 저장소에 올리기 전에 직접 한 번 더 확인하세요.
- **flake의 함정을 처리합니다.** flake는 git이 추적하는 파일만 보므로, 새로 옮긴 dotfile을 자동으로 `git add`합니다. 커밋과 푸시는 요청할 때만 하며(`ehm commit`, TUI에서는 `m`), 추적 중인 파일과 `dotfiles/`, 새 `.nix` 파일만 포함합니다. 그 밖의 추적되지 않은 파일은 비밀 정보가 있을 수 있어 빼 둡니다.

## dotfile 방식

| | 기본 (링크) | `--copy` |
|---|---|---|
| 수정 반영 | 저장소 파일을 고치면 바로 | `ehm apply` 후 |
| 앱이 파일을 고칠 수 있나 | 예 | 아니요 (읽기 전용) |
| 저장소 위치가 바뀌면 | 다시 설정 필요 | 영향 없음 |

## 지원하는 구성

자동으로 감지합니다.

| 구성 | 적용 명령 | 미리보기·옵션 설정 |
|---|---|---|
| NixOS 모듈 + flake | `sudo nixos-rebuild switch --flake` | 지원 |
| 독립 실행 + flake | `home-manager switch --flake` | 지원 |
| NixOS 모듈 + 채널 | `sudo nixos-rebuild switch` | 미지원 |
| 독립 실행 + 채널 | `home-manager switch` | 미지원 |

NixOS 모듈 구성에서 `apply`와 `rollback`은 시스템 전체에 적용됩니다. 설정이 없으면 `ehm init`으로 독립 실행 flake를 새로 만들 수 있습니다.

`lib.mkMerge`처럼 정적으로 펼칠 수 없는 구조는 추측해서 고치지 않고, 직접 수정하도록 안내합니다.

## ehm 자체 설정

`~/.config/ehm/config.toml` (모두 선택 사항, `ehm config`로 안내를 볼 수 있습니다)

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

## 개발

```bash
nix develop                  # Textual이 포함된 Python과 ruff, PYTHONPATH=src 설정
python -m unittest discover -s tests
python -m ehm status
ruff check src tests
```

Textual이 없으면 TUI 테스트는 건너뜁니다.
