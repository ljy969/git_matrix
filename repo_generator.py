# -*- coding: utf-8 -*-
"""根据 pattern.json 生成包含伪造提交历史的 Git 仓库。

用法:
    python repo_generator.py [配置文件路径] [--force]

说明:
    - 配置文件默认取当前目录下的 pattern.json
    - --force 用于目标目录已经是 Git 仓库时，在其基础上追加提交
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

# 仓库名白名单：仅字母、数字、点、下划线、连字符（从根本上杜绝路径穿越）
REPO_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# 单日提交数上限，避免误配置导致近乎无限地生成提交
MAX_COMMITS_PER_DAY = 100
# 提交时间固定为当天 12:00 UTC：无论本机时区如何，日期都落在同一天
COMMIT_TIME = 'T12:00:00+00:00'


def setup_output_encoding():
    """让中文/emoji 在任意控制台（含 cp1252 等西文编码）下都不会崩溃。"""
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            try:
                # 无法切换编码时，至少保证不会因编码异常而崩溃
                stream.reconfigure(errors='replace')
            except Exception:
                pass


def safe_write(text):
    """向 stdout 写文本；即使当前编码无法表示某些字符也不会抛异常。"""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
        sys.stdout.write(text.encode(encoding, 'replace').decode(encoding, 'replace'))
        sys.stdout.flush()


def check_git_installed():
    """检查系统中是否安装了 Git."""
    if shutil.which("git") is None:
        logging.error("系统中未找到 'git' 命令。")
        logging.error("请先安装 Git 然后再运行此脚本: https://git-scm.com/downloads")
        return False
    return True


def run_command(command, cwd, env=None):
    """在指定目录下运行命令。

    command 一律使用参数列表形式且不使用 shell，避免命令注入与引号拼接问题。
    """
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
        )
    except subprocess.CalledProcessError as e:
        logging.error("命令执行失败: %s", ' '.join(command))
        logging.error("返回码: %s", e.returncode)
        if e.stdout and e.stdout.strip():
            logging.error("输出: %s", e.stdout.strip())
        if e.stderr and e.stderr.strip():
            logging.error("错误: %s", e.stderr.strip())
        return None
    except FileNotFoundError:
        logging.error("命令执行失败: %s 未找到。请确保它在系统的 PATH 中。", command[0])
        return None
    except OSError as e:
        logging.error("无法执行命令 %s: %s", ' '.join(command), e)
        return None


def load_config(config_path):
    """读取并解析配置文件，返回 (config, 错误信息)。"""
    try:
        # utf-8-sig 同时兼容带 BOM 的 UTF-8 文件
        with open(config_path, 'r', encoding='utf-8-sig') as f:
            config = json.load(f)
    except FileNotFoundError:
        return None, f"找不到配置文件 '{config_path}'。"
    except json.JSONDecodeError as e:
        return None, f"配置文件 '{config_path}' 不是合法的 JSON: {e}"
    except UnicodeDecodeError as e:
        return None, f"配置文件 '{config_path}' 不是 UTF-8 编码: {e}"
    except OSError as e:
        return None, f"无法读取配置文件 '{config_path}': {e}"

    if not isinstance(config, dict):
        return None, "配置文件的最外层必须是一个 JSON 对象。"
    return config, None


def validate_repo_name(repo_name):
    """校验仓库名，防止路径穿越与非法字符。"""
    if not isinstance(repo_name, str) or not repo_name.strip():
        return "仓库名称不能为空。"
    if repo_name in ('.', '..') or not REPO_NAME_RE.match(repo_name):
        return ("仓库名称只能包含字母、数字、点、下划线或连字符，且不能包含路径分隔符。"
                f"当前值: {repo_name!r}")
    return None


def parse_levels(raw_levels):
    """校验 levels 配置，返回 (levels, 错误信息)。"""
    if not isinstance(raw_levels, dict) or not raw_levels:
        return None, "levels 必须是非空对象，例如 {\"0\": 0, \"1\": 1, \"2\": 5, \"3\": 10, \"4\": 20}。"
    levels = {}
    for key, value in raw_levels.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None, f"levels['{key}'] 必须是整数，当前为 {value!r}。"
        if count < 0:
            return None, f"levels['{key}'] 不能为负数（当前 {count}）。"
        if count > MAX_COMMITS_PER_DAY:
            return None, (f"levels['{key}'] = {count} 超过单日上限 {MAX_COMMITS_PER_DAY}。"
                          "该数值会生成海量提交，请在配置中调低。")
        levels[str(key)] = count
    return levels, None


def validate_pattern(pattern, levels):
    """校验 pattern 结构，返回 (num_rows, num_cols, 错误信息)。"""
    if not isinstance(pattern, list) or not pattern:
        return 0, 0, "pattern 必须是非空的字符串数组。"
    if not all(isinstance(row, str) for row in pattern):
        return 0, 0, "pattern 的每一行都必须是字符串。"
    widths = {len(row) for row in pattern}
    if len(widths) != 1:
        return 0, 0, f"pattern 各行长度必须一致（当前各行的宽度为: {sorted(widths)}）。"
    num_rows = len(pattern)
    num_cols = widths.pop()
    if num_rows != 7 or num_cols != 53:
        logging.warning("pattern 尺寸为 %sx%s，预期为 7x53（一天 7 行、一年最多 53 列）。",
                        num_rows, num_cols)
    unknown = sorted({ch for row in pattern for ch in row if ch not in levels})
    if unknown:
        return 0, 0, ("pattern 中出现了 levels 未定义的等级字符: "
                      + ' '.join(repr(ch) for ch in unknown)
                      + "，请在 levels 中补充定义。")
    return num_rows, num_cols, None


def get_existing_commit_count(repo_path):
    """返回仓库现有提交数；无法读取（例如尚未有任何提交）时返回 0。"""
    try:
        result = subprocess.run(
            ['git', 'rev-list', '--count', 'HEAD'],
            cwd=repo_path,
            text=True,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
        )
    except OSError:
        return 0
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def create_repo_from_pattern(config_path, force=False):
    """从配置文件生成仓库。成功返回 True，失败返回 False。"""
    if not check_git_installed():
        return False

    # --- 1. 读取和校验配置文件 ---
    config, error = load_config(config_path)
    if error:
        logging.error(error)
        return False

    git_config = config.get("git_config")
    if not isinstance(git_config, dict):
        git_config = {}
    repo_name = config.get("repository_name")
    user_name = git_config.get("user_name") or "User"
    user_email = git_config.get("user_email") or "user@example.com"
    start_date_str = config.get("start_date")
    pattern = config.get("pattern")

    missing = [name for name, value in (("repository_name", repo_name),
                                        ("start_date", start_date_str),
                                        ("pattern", pattern)) if not value]
    if missing:
        logging.error("配置文件缺少必要的字段: %s。", ', '.join(missing))
        return False

    error = validate_repo_name(repo_name)
    if error:
        logging.error(error)
        return False

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        logging.error("start_date 必须是 YYYY-MM-DD 格式的日期，当前为 %r。", start_date_str)
        return False

    levels, error = parse_levels(config.get("levels"))
    if error:
        logging.error(error)
        return False

    num_rows, num_cols, error = validate_pattern(pattern, levels)
    if error:
        logging.error(error)
        return False

    if not EMAIL_RE.match(str(user_email)):
        logging.warning("Git 邮箱 '%s' 格式看起来不正确，GitHub 可能不会将其计入贡献图。", user_email)

    # --- 2. 准备并检查目标目录 ---
    repo_path = os.path.abspath(repo_name)
    if os.path.exists(repo_path) and not os.path.isdir(repo_path):
        logging.error("目标路径 '%s' 已被同名文件占用，请更换仓库名称或先删除该文件。", repo_name)
        return False

    existing_commits = 0
    if os.path.isdir(repo_path):
        existing_commits = get_existing_commit_count(repo_path)
        if existing_commits > 0 and not force:
            logging.error("目录 '%s' 已存在，且其中已有 %s 个提交。", repo_name, existing_commits)
            logging.error("为避免重复提交，已停止。若要重新生成，请先删除该目录；"
                          "若要在其基础上追加提交，请加上 --force 参数。")
            return False
        if existing_commits > 0:
            logging.warning("--force 已启用：将在现有 %s 个提交之后继续追加。", existing_commits)
    else:
        os.makedirs(repo_path)
    logging.info("正在仓库 '%s' 中进行初始化...", repo_name)
    if run_command(['git', 'init'], cwd=repo_path) is None:
        return False
    if run_command(['git', 'config', 'user.name', str(user_name)], cwd=repo_path) is None:
        return False
    if run_command(['git', 'config', 'user.email', str(user_email)], cwd=repo_path) is None:
        return False

    # --- 3. 循环生成提交 ---
    commits_this_run = 0
    readme_path = os.path.join(repo_path, 'README.md')

    for col in range(num_cols):
        for row in range(num_rows):
            level_char = pattern[row][col]
            commit_count_for_day = levels[level_char]

            if commit_count_for_day > 0:
                current_date = start_date + timedelta(days=(col * 7 + row))
                commit_date = current_date.strftime('%Y-%m-%d') + COMMIT_TIME

                for _ in range(commit_count_for_day):
                    commits_this_run += 1
                    # 追加模式下编号从已有提交之后继续，保证内容与提交信息唯一
                    sequence_no = existing_commits + commits_this_run

                    # a. 修改文件
                    with open(readme_path, 'w', encoding='utf-8') as f:
                        f.write(f"Commit #{sequence_no} on {current_date.date()}\n")

                    # b. 添加文件到暂存区
                    if run_command(['git', 'add', 'README.md'], cwd=repo_path) is None:
                        return False

                    # c. 使用特定日期进行提交
                    env = os.environ.copy()
                    env['GIT_AUTHOR_DATE'] = commit_date
                    env['GIT_COMMITTER_DATE'] = commit_date

                    # --allow-empty：即使 README 内容与上次提交完全相同也能成功提交
                    commit_message = f"feat: commit #{sequence_no}"
                    if run_command(['git', 'commit', '--allow-empty', '-m', commit_message],
                                   cwd=repo_path, env=env) is None:
                        return False

            # 打印进度 (使用 sys.stdout 来实现单行刷新)
            processed_days = col * num_rows + row + 1
            total_days = num_cols * num_rows
            progress = processed_days / total_days * 100
            safe_write(f"\r处理进度: {progress:.1f}% ({processed_days}/{total_days} 天), "
                       f"已生成 {commits_this_run} 次提交...")

    safe_write('\n')

    if commits_this_run == 0:
        logging.warning("图案为空（没有任何非零等级格），仓库已初始化但未生成任何提交。")
        logging.warning("空仓库无法推送（git push 会失败），请先在网页中设计图案后重新生成。")
        return False

    if existing_commits:
        logging.info("处理完成！本次新增 %s 次提交（仓库累计 %s 次）。",
                     commits_this_run, existing_commits + commits_this_run)
    else:
        logging.info("处理完成！总共生成了 %s 次提交。", commits_this_run)

    # --- 4. 显示后续操作指南 ---
    instructions = f"""
        {'='*60}
        Git 仓库已成功在本地生成!

        下一步操作:
        1. 前往 GitHub 创建一个新的 **空** 仓库 (不要勾选'Add a README file').
        2. 在你的终端中，进入刚刚生成的目录并执行以下命令:

            cd "{repo_name}"
            git remote add origin <你的远程仓库URL>
            git branch -M main
            git push -u origin main

        推送完成后，稍等片刻，你的 GitHub 贡献图就会更新！
        提交日期统一使用 UTC 当天 12:00，可避免因时区差异导致的日期错位。
        {'='*60}
        """
    safe_write(instructions)
    return True


def main(argv=None):
    setup_output_encoding()
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s',
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(
        description="根据 pattern.json 生成包含伪造提交历史的 Git 仓库。")
    parser.add_argument('config', nargs='?', default='pattern.json',
                        help="配置文件路径（默认为当前目录下的 pattern.json）")
    parser.add_argument('--force', action='store_true',
                        help="目标目录已存在 Git 仓库时，允许在其基础上追加提交")
    args = parser.parse_args(argv)

    ok = create_repo_from_pattern(args.config, force=args.force)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
