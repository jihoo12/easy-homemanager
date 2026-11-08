# ehm — easy Home Manager

[English](README.md) · **한국어**

Home Manager 설정을 **반자동으로** 관리하는 CLI입니다. `.nix` 문법을 몰라도 패키지를 설치하고, 프로그램 옵션을 고르고, dotfile을 옮길 수 있습니다. 설정이 어수선하면 분석해서 개선안을 제안합니다.

CLI 메시지는 영어로 출력됩니다. 고친 결과는 평범한 `home.nix`로 남습니다. 주석과 서식을 유지하며 필요한 줄만 바꾸므로, 나중에 `ehm` 없이 직접 관리해도 됩니다.

## 시작하기

```bash
nix run path:. -- status     # 설치 없이 실행 (이 폴더에서)
nix run path:.               # 메뉴 열기
```

Home Manager 설정에 넣어 계속 쓰려면 flake 입력에 이 저장소를 추가하고 `home.packages`에 `inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default`를 넣으세요.

`nix-command`와 `flakes`는 `ehm`이 알아서 켜므로 따로 설정하지 않아도 됩니다. 의존성은 Python 표준 라이브러리뿐입니다(3.11 이상).

## 무엇을 할 수 있나요

```bash
ehm                          # 대화형 메뉴
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
ehm rollback                 # 이전 세대로
ehm gc                       # 오래된 세대 정리
```

저장을 묻는 명령은 `--apply`로 저장 후 바로 적용할 수 있고, `-y`로 확인을 건너뛸 수 있습니다.

## 안전장치

- **먼저 보여 줍니다.** 모든 수정은 diff를 보여 주고 확인을 받습니다.
- **저장 후 바로 검사합니다.** 문법과 옵션을 Nix로 평가하고, 실패하면 되돌릴 수 있습니다. `-y`로 실행했다면 자동으로 되돌립니다.
- **적용 전에 빌드합니다.** Home Manager 세대를 sudo 없이 먼저 빌드해 설정 오류를 잡고, 바뀌는 패키지와 파일을 보여 줍니다.
- **파일을 지우지 않습니다.** 충돌 파일은 `~/.local/state/ehm/backups/`로 옮기고, 적용에 실패하면 되돌립니다. 저장소로 옮긴 파일이 아닌 충돌은 허락을 받은 뒤에만 옮깁니다.
- **비밀 정보를 막습니다.** 개인 키, 토큰, 비밀번호로 보이는 파일과 `~/.ssh` 같은 폴더는 `dot add`를 거부합니다. 검사는 패턴 기반이라 놓칠 수 있으니, 공개 저장소에 올리기 전에 직접 한 번 더 확인하세요.
- **flake의 함정을 처리합니다.** flake는 git이 추적하는 파일만 보므로, 새로 옮긴 dotfile을 자동으로 `git add`합니다. 커밋과 푸시는 요청할 때만 합니다.

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
auto_commit = false
auto_push = false
```

## 개발

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m ehm status
```
