#!/bin/sh
# entrypoint.sh — 容器启动入口
#
# 职责：确保 config 文件是一个可用的**普通文件**再启动服务，否则宁可 FATAL 退出，
# 也不让应用带着坏配置进入无限崩溃重启。不做其他事（不改业务逻辑）。
#
# 问题背景（Windows/WSL2 fresh install 崩溃重启）：
#   旧 compose 用单文件 bind mount `./config.yaml:/app/config.yaml`。若宿主
#   ./config.yaml 不存在，Docker（尤其 Windows/WSL2）会把它当成目录创建并挂进来，
#   /app/config.yaml 于是是个**目录**而非文件，应用读它直接 IsADirectoryError 崩溃。
#   更糟的是：bind mount 的挂载点在容器内**删不掉**（rm 报 "Device or resource busy"）。
#   根治办法是不再单文件挂载 config，改用 $OMBRE_CONFIG_PATH 把配置放进已经是目录挂载
#   的数据卷里（见 docker-compose.user.yml）。本脚本是最后一道防线。
#
# 处理逻辑：
#   1. 配置路径取 $OMBRE_CONFIG_PATH，未设则退回 /app/config.yaml（老行为，兼容现有部署）。
#   2. 确保父目录存在。
#   3. 若该路径已是目录，立即 fail closed。配置路径来自环境变量，绝不能对它
#      `rm -rf`/`find -delete`；误指到记忆卷时那会删掉用户数据。
#   4. 路径不存在则从内置默认模板初始化。
#   5. 最终校验：路径必须是普通文件，否则打印清晰指引并 FATAL 退出（不带病启动）。

IMAGE_ROOT="${OMBRE_IMAGE_ROOT:-/app}"
CONFIG="${OMBRE_CONFIG_PATH:-$IMAGE_ROOT/config.yaml}"
DEFAULT="$IMAGE_ROOT/config.default.yaml"

mkdir -p "$(dirname "$CONFIG")" 2>/dev/null || true

# --- 3. 目录绝不自动删除：它可能就是整个记忆卷 ---
if [ -d "$CONFIG" ]; then
    echo "[entrypoint] '$CONFIG' is a directory (Docker created it because the host file was missing)."
    echo "[entrypoint] FATAL: refusing to delete a directory supplied as OMBRE_CONFIG_PATH."
    echo "[entrypoint] Point OMBRE_CONFIG_PATH at a file, for example:"
    echo "[entrypoint]     /app/buckets/config.yaml"
    exit 1
fi

# --- 4. 不存在则从默认模板初始化 ---
if [ ! -e "$CONFIG" ]; then
    echo "[entrypoint] Initializing config from defaults at '$CONFIG'..."
    cp "$DEFAULT" "$CONFIG"
fi

# --- 5. 最终校验：必须是普通文件，否则别启动去无限崩溃刷屏 ---
if [ ! -f "$CONFIG" ]; then
    echo "[entrypoint] FATAL: could not prepare a usable config file at '$CONFIG'."
    echo "[entrypoint] Two known causes:"
    echo "[entrypoint]   (a) compose single-file-mounts a missing config (Docker makes it a directory):"
    echo "[entrypoint]         volumes:  - ./config.yaml:/app/config.yaml   <-- remove this line"
    echo "[entrypoint]   (b) the path sits on a read-only / non-writable filesystem (many PaaS, e.g."
    echo "[entrypoint]         Zeabur, use a read-only rootfs — only the mounted volume is writable)."
    echo "[entrypoint] Fix: point config at the writable data volume:"
    echo "[entrypoint]     environment:  - OMBRE_CONFIG_PATH=/app/buckets/config.yaml"
    echo "[entrypoint]     volumes:      - ./buckets:/app/buckets   (PaaS: mount the volume at /app/buckets)"
    echo "[entrypoint] The image already defaults OMBRE_CONFIG_PATH to /app/buckets/config.yaml;"
    echo "[entrypoint] this FATAL means it was overridden to an unwritable location."
    exit 1
fi

echo "[entrypoint] config ready at '$CONFIG'."

# ============================================================
# 持久化代码 bootstrap（#4a ①②）
# ------------------------------------------------------------
# 问题：容器平台(Docker/Render/Zeabur)代码烤在镜像只读层，/api/do-update 的
# 热更新写在可写临时层，容器一重建就回退到旧镜像 → 更新"留不住"。
# 方案：把 src/frontend 播种到数据卷上的 CODE_DIR，从那里运行；repo_root 随之
# 指向 CODE_DIR，热更新直接写持久盘，容器重建也还在。
#   · 镜像版本变化(正经重建) → 重新播种，让重建覆盖旧热更新。
#   · 连续启动失败 → 回滚到 _prev（do-update 覆盖前留的上一版），防坏更新锁死。
#   · 任何环节失败 → 回退到镜像内置 /app/src，绝不 brick。
# 裸机(非 Docker)不走本脚本，直接从仓库目录跑，本就是持久的。
# ============================================================
CODE_DIR="${OMBRE_CODE_DIR:-$(dirname "$CONFIG")/_app}"
RUN_ROOT="$IMAGE_ROOT"
ROLLBACK_THRESHOLD=2
FINGERPRINT_TOOL="$IMAGE_ROOT/src/ombrebrain/maintenance/code_fingerprint.py"

_code_fingerprint() {
    [ -f "$FINGERPRINT_TOOL" ] || return 1
    python "$FINGERPRINT_TOOL" "$1" 2>/dev/null
}

_write_marker() {
    marker="$1"
    value="$2"
    marker_tmp="$marker.tmp.$$"
    printf '%s\n' "$value" > "$marker_tmp" 2>/dev/null || return 1
    mv -f "$marker_tmp" "$marker" 2>/dev/null
}

# 随镜像播种到运行时目录的可选顶层目录。src / frontend 是必需的（缺了跑不起来，
# 单独处理），这里只列「有就播种、没有也不影响启动」的：
#   docs/   —— adr_requirements 诊断在运行时目录下读 docs/adr/
#   tools/  —— preflight_cli_diagnostics 诊断要求 tools/vnext_preflight.py
#   kernel/ —— vnext_preflight 的 rust_kernel_scaffold 契约检查对象
# 此前不播种它们，这三项诊断在任何 Docker 部署上都恒红。老镜像没有这些目录，
# 因此一律「存在才处理」，绝不能让缺失阻断启动或回滚。
SEED_DIRS_OPTIONAL="docs tools kernel"

_restore_seed_swap() {
    old_dir="$1"
    rm -rf "$CODE_DIR/src" "$CODE_DIR/frontend" 2>/dev/null || true
    [ ! -d "$old_dir/src" ] || mv "$old_dir/src" "$CODE_DIR/src" 2>/dev/null || true
    [ ! -d "$old_dir/frontend" ] || mv "$old_dir/frontend" "$CODE_DIR/frontend" 2>/dev/null || true
    # 可选目录：回滚到旧状态——旧状态没有就该删掉新播种进来的，不做恢复。
    for seed_dir in $SEED_DIRS_OPTIONAL; do
        rm -rf "$CODE_DIR/$seed_dir" 2>/dev/null || true
        [ ! -d "$old_dir/$seed_dir" ] || mv "$old_dir/$seed_dir" "$CODE_DIR/$seed_dir" 2>/dev/null || true
    done
    for root_file in VERSION requirements.txt requirements.lock.txt; do
        rm -f "$CODE_DIR/$root_file" 2>/dev/null || true
        [ ! -f "$old_dir/$root_file" ] || cp -a "$old_dir/$root_file" "$CODE_DIR/$root_file" 2>/dev/null || true
    done
}

_seed_image_code() {
    # Copy and validate first. The active tree is not touched until the stage is complete.
    stage="$CODE_DIR/.seed-next.$$"
    old_dir="$CODE_DIR/.seed-old.$$"
    rm -rf "$stage" "$old_dir" 2>/dev/null || true
    mkdir -p "$stage" "$old_dir" 2>/dev/null || return 1
    cp -a "$IMAGE_ROOT/src" "$stage/src" 2>/dev/null || { rm -rf "$stage" "$old_dir"; return 1; }
    cp -a "$IMAGE_ROOT/frontend" "$stage/frontend" 2>/dev/null || { rm -rf "$stage" "$old_dir"; return 1; }
    # 可选目录：镜像里有才复制；复制失败说明镜像损坏，按整体失败处理。
    for seed_dir in $SEED_DIRS_OPTIONAL; do
        [ ! -d "$IMAGE_ROOT/$seed_dir" ] || cp -a "$IMAGE_ROOT/$seed_dir" "$stage/$seed_dir" 2>/dev/null || {
            rm -rf "$stage" "$old_dir"
            return 1
        }
    done
    for root_file in VERSION requirements.txt requirements.lock.txt; do
        [ ! -f "$IMAGE_ROOT/$root_file" ] || cp -a "$IMAGE_ROOT/$root_file" "$stage/$root_file" 2>/dev/null || {
            rm -rf "$stage" "$old_dir"
            return 1
        }
    done
    [ -f "$stage/src/server.py" ] && [ -d "$stage/frontend" ] || {
        rm -rf "$stage" "$old_dir" 2>/dev/null
        return 1
    }

    # Keep the last known-good active tree while swapping the two top-level trees.
    [ ! -d "$CODE_DIR/src" ] || mv "$CODE_DIR/src" "$old_dir/src" 2>/dev/null || {
        rm -rf "$stage" "$old_dir" 2>/dev/null
        return 1
    }
    if [ -d "$CODE_DIR/frontend" ]; then
        mv "$CODE_DIR/frontend" "$old_dir/frontend" 2>/dev/null || {
            _restore_seed_swap "$old_dir"
            rm -rf "$stage" "$old_dir" 2>/dev/null
            return 1
        }
    fi
    # 到这里 src（可能还有 frontend）已经移进 old_dir，回滚要走 _restore_seed_swap。
    for seed_dir in $SEED_DIRS_OPTIONAL; do
        [ ! -d "$CODE_DIR/$seed_dir" ] || mv "$CODE_DIR/$seed_dir" "$old_dir/$seed_dir" 2>/dev/null || {
            _restore_seed_swap "$old_dir"
            rm -rf "$stage" "$old_dir" 2>/dev/null
            return 1
        }
    done
    for root_file in VERSION requirements.txt requirements.lock.txt; do
        [ ! -f "$CODE_DIR/$root_file" ] || cp -a "$CODE_DIR/$root_file" "$old_dir/$root_file" 2>/dev/null || true
    done

    mv "$stage/src" "$CODE_DIR/src" 2>/dev/null || {
        _restore_seed_swap "$old_dir"
        rm -rf "$stage" "$old_dir" 2>/dev/null
        return 1
    }
    mv "$stage/frontend" "$CODE_DIR/frontend" 2>/dev/null || {
        _restore_seed_swap "$old_dir"
        rm -rf "$stage" "$old_dir" 2>/dev/null
        return 1
    }
    for seed_dir in $SEED_DIRS_OPTIONAL; do
        [ ! -d "$stage/$seed_dir" ] || mv "$stage/$seed_dir" "$CODE_DIR/$seed_dir" 2>/dev/null || {
            _restore_seed_swap "$old_dir"
            rm -rf "$stage" "$old_dir" 2>/dev/null
            return 1
        }
    done
    for root_file in VERSION requirements.txt requirements.lock.txt; do
        rm -f "$CODE_DIR/$root_file" 2>/dev/null || true
        [ ! -f "$stage/$root_file" ] || cp -a "$stage/$root_file" "$CODE_DIR/$root_file" 2>/dev/null || {
            _restore_seed_swap "$old_dir"
            rm -rf "$stage" "$old_dir" 2>/dev/null || true
            return 1
        }
    done
    rm -rf "$stage" 2>/dev/null || true

    # A previously healthy runtime becomes the crash rollback point for this image seed.
    if [ "$FAILS" -eq 0 ] && [ -f "$old_dir/src/server.py" ]; then
        rm -rf "$CODE_DIR/_prev" 2>/dev/null || true
        mv "$old_dir" "$CODE_DIR/_prev" 2>/dev/null || rm -rf "$old_dir" 2>/dev/null || true
    else
        rm -rf "$old_dir" 2>/dev/null || true
    fi
    return 0
}

_bootstrap_code() {
    # CODE_DIR 必须在可写持久卷上；试探写权限，失败就回退镜像代码。
    mkdir -p "$CODE_DIR" 2>/dev/null || return 1
    ( : > "$CODE_DIR/.wtest" ) 2>/dev/null || return 1
    rm -f "$CODE_DIR/.wtest" 2>/dev/null || true

    IMG_VER="$(cat "$IMAGE_ROOT/VERSION" 2>/dev/null || echo unknown)"
    SEEDED_VER="$(cat "$CODE_DIR/.seeded_image_version" 2>/dev/null || echo none)"
    IMG_FP="$(_code_fingerprint "$IMAGE_ROOT" 2>/dev/null || true)"
    SEEDED_FP="$(cat "$CODE_DIR/.seeded_image_fingerprint" 2>/dev/null || echo none)"

    # A separate code volume can leave the historical <data>/_app behind. Never
    # delete it automatically: only identify it clearly so operators do not inspect
    # an inactive VERSION and mistake it for the running process.
    DATA_ROOT="${OMBRE_BUCKETS_DIR:-$(dirname "$CONFIG")}"
    LEGACY_CODE_DIR="$DATA_ROOT/_app"
    ACTIVE_REAL="$(cd "$CODE_DIR" 2>/dev/null && pwd -P || echo "$CODE_DIR")"
    if [ -d "$LEGACY_CODE_DIR" ]; then
        LEGACY_REAL="$(cd "$LEGACY_CODE_DIR" 2>/dev/null && pwd -P || echo "$LEGACY_CODE_DIR")"
        if [ "$ACTIVE_REAL" != "$LEGACY_REAL" ] && [ -f "$LEGACY_CODE_DIR/src/server.py" ]; then
            LEGACY_VER="$(cat "$LEGACY_CODE_DIR/VERSION" 2>/dev/null || echo unknown)"
            echo "[entrypoint] WARNING code-state=legacy-residue: 检测到旧布局代码遗留 $LEGACY_CODE_DIR (v$LEGACY_VER)；未被当前进程使用，可在确认备份后删除。"
        fi
    fi
    echo "[entrypoint] 活动代码目录: $ACTIVE_REAL"

    # --- ② 崩溃自愈：上一次启动没被 server.py 标记成功 → 计数累加；超阈值且有 _prev 则回滚 ---
    FAILS="$(cat "$CODE_DIR/.boot_fails" 2>/dev/null || echo 0)"
    case "$FAILS" in ''|*[!0-9]*) FAILS=0 ;; esac
    if [ "$FAILS" -ge "$ROLLBACK_THRESHOLD" ] && [ -f "$CODE_DIR/_prev/src/server.py" ]; then
        echo "[entrypoint] 连续 $FAILS 次启动失败 → 回滚到上一版代码 (_prev)"
        rm -rf "$CODE_DIR/src" "$CODE_DIR/frontend" 2>/dev/null
        cp -a "$CODE_DIR/_prev/src" "$CODE_DIR/src" 2>/dev/null || return 1
        cp -a "$CODE_DIR/_prev/frontend" "$CODE_DIR/frontend" 2>/dev/null || true
        # 可选目录：回滚点没有就该删掉，让运行时树与 _prev 完全一致。
        for seed_dir in $SEED_DIRS_OPTIONAL; do
            rm -rf "$CODE_DIR/$seed_dir" 2>/dev/null || true
            [ ! -d "$CODE_DIR/_prev/$seed_dir" ] || cp -a "$CODE_DIR/_prev/$seed_dir" "$CODE_DIR/$seed_dir" 2>/dev/null || true
        done
        for root_file in VERSION requirements.txt requirements.lock.txt; do
            rm -f "$CODE_DIR/$root_file" 2>/dev/null || true
            [ ! -f "$CODE_DIR/_prev/$root_file" ] || cp -a "$CODE_DIR/_prev/$root_file" "$CODE_DIR/$root_file" 2>/dev/null || return 1
        done
        rm -rf "$CODE_DIR/_prev" 2>/dev/null
        FAILS=0
        echo 0 > "$CODE_DIR/.boot_fails" 2>/dev/null || true
        # Treat the rollback as a persisted override of this image baseline. Without
        # refreshing these markers, a missing/old fingerprint marker would immediately
        # reseed the same failed image and undo the rollback.
        _write_marker "$CODE_DIR/.seeded_image_version" "$IMG_VER" || true
        [ -z "$IMG_FP" ] || _write_marker "$CODE_DIR/.seeded_image_fingerprint" "$IMG_FP" || true
        SEEDED_VER="$IMG_VER"
        [ -z "$IMG_FP" ] || SEEDED_FP="$IMG_FP"
    fi

    # --- ① 播种 / 重建覆盖：版本或镜像代码指纹任一变化都会换代。---
    # Compare against the fingerprint captured at the previous image seed, not the
    # current runtime tree. Dashboard hot updates intentionally change the runtime
    # tree and must survive restarts while the underlying image remains unchanged.
    RESEED_REASON=""
    RESEED_CODE=""
    case "${OMBRE_FORCE_CODE_RESEED:-0}" in
        1|true|TRUE|yes|YES|on|ON)
            RESEED_REASON="管理员要求强制重新播种"
            RESEED_CODE="forced"
            ;;
    esac
    if [ -z "$RESEED_REASON" ] && [ ! -f "$CODE_DIR/src/server.py" ]; then
        RESEED_REASON="运行代码缺失"
        RESEED_CODE="runtime-missing"
    elif [ -z "$RESEED_REASON" ] && [ "$IMG_VER" != "$SEEDED_VER" ]; then
        RESEED_REASON="镜像版本变化 ($SEEDED_VER -> $IMG_VER)"
        RESEED_CODE="image-version-changed"
    elif [ -z "$RESEED_REASON" ] && [ -n "$IMG_FP" ] && [ "$IMG_FP" != "$SEEDED_FP" ]; then
        RESEED_REASON="镜像代码指纹变化（VERSION 未变也会更新）"
        RESEED_CODE="image-fingerprint-changed"
    fi

    if [ -n "$RESEED_REASON" ]; then
        # ${CODE_DIR} 的花括号不能省：后面紧跟的全角「：」是多字节字符，
        # shell 会把它的首字节当成变量名的一部分去展开一个不存在的变量，
        # 结果是路径整个丢失、并向 stdout 吐出半个字符（非法 UTF-8）。
        # ${CODE_DIR} 的花括号一个都不能省。这里原本写的 $CODE_DIR：，
        # 全角冒号的第一个字节被当成变量名的一部分吃掉了，路径没了，
        # 还顺手吐出一串非法 UTF-8。四个测试为此红了不知道多久，
        # 谁看日志都觉得是路径配错了。是个冒号。
        echo "[entrypoint] code-state=reseed reason=$RESEED_CODE: 播种代码到持久卷 ${CODE_DIR}：$RESEED_REASON"
        _seed_image_code || return 1
        _write_marker "$CODE_DIR/.seeded_image_version" "$IMG_VER" || return 1
        [ -z "$IMG_FP" ] || _write_marker "$CODE_DIR/.seeded_image_fingerprint" "$IMG_FP" || return 1
        FAILS=0
    elif [ -z "$IMG_FP" ]; then
        echo "[entrypoint] WARNING: 无法计算镜像代码指纹，当前仅按 VERSION 判断是否重新播种。"
    fi

    [ -f "$CODE_DIR/src/server.py" ] || return 1

    RUN_FP="$(_code_fingerprint "$CODE_DIR" 2>/dev/null || true)"
    if [ -n "$IMG_FP" ] && [ -n "$RUN_FP" ]; then
        if [ "$RUN_FP" = "$IMG_FP" ]; then
            echo "[entrypoint] code-state=image-match: 运行代码与镜像指纹一致 ($(printf '%.12s' "$IMG_FP"))。"
        else
            echo "[entrypoint] code-state=runtime-override: 运行代码与镜像基线不同（热更新或自动回滚），保留卷内版本。"
        fi
    fi

    # 预增启动失败计数；启动成功后 server.py 会清零，崩溃则保留 → 下次累加直至回滚。
    echo $((FAILS + 1)) > "$CODE_DIR/.boot_fails" 2>/dev/null || true
    RUN_ROOT="$CODE_DIR"
    return 0
}

if _bootstrap_code; then
    echo "[entrypoint] 从持久卷运行: $RUN_ROOT/src/server.py"
else
    echo "[entrypoint] 持久卷代码不可用，回退到镜像内置代码 /app/src（不影响本次运行）"
    RUN_ROOT="$IMAGE_ROOT"
fi

if [ "${OMBRE_BOOTSTRAP_ONLY:-0}" = "1" ]; then
    # Test/diagnostic mode: reaching this point is equivalent to a successful boot.
    [ "$RUN_ROOT" = "$IMAGE_ROOT" ] || echo 0 > "$RUN_ROOT/.boot_fails" 2>/dev/null || true
    echo "[entrypoint] bootstrap-only complete; service process was not started."
    exit 0
fi

cd "$RUN_ROOT" 2>/dev/null || cd /app
exec python src/server.py
