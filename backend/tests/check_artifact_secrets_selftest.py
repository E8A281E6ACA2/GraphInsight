#!/usr/bin/env python3
"""敏感信息扫描器的正样本自检与路径排除守卫（任务 #31）。

为什么必须有这套自检：`check_artifact_secrets.py` 在 CI 里只做一件事——没有命中就
放行产物。这意味着"规则被改坏成什么都不命中"会表现成永久绿灯，是最危险的失效形态
（R2 评审因此点名：只有负样本扫描，没有正样本自检）。本套件反过来断言：喂已知凭据
必须 findings>0，喂结构性样本必须 findings=0，并验证路径排除只削弱赋值形状、
不削弱注入凭据字面值。

取证口径：
  * 每个样本单独落一个临时文件、单独跑一次 CLI，失败时能直接指到是哪条形状退化，
    而不是只知道"总数不对"。
  * 断言只看退出码与 `kind=` 标签，绝不把样本凭据写进期望值比对，避免自检本身回显凭据。

运行：python backend/tests/check_artifact_secrets_selftest.py
（只用标准库 + 临时目录；不连任何服务，不读 .env）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
SCANNER = backend_dir / "tests" / "check_artifact_secrets.py"

FAILURES: list[str] = []

# 注入凭据字面值：长度与形状都刻意不像"通用凭据形状"，只可能被 run_credential 层抓到，
# 因此它能单独证明字面值层在排除目录里仍然生效。
RUN_LITERAL_ENV = "GRAPHINSIGHT_SELFTEST_RUN_CREDENTIAL"
RUN_LITERAL = "selftest-once-credential-4f0c9b7e21"

BCRYPT_SAMPLE = "$2b$12$N9u6E9lOZ3xK4mQ7pRsT2vW8yB5cD1fG6hJ0kL3mN4oP7qR9sT2uV"
JWT_SAMPLE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5E_Xw"

TMP: Path = None  # type: ignore[assignment]


def _utf8_env(base: dict, extra: dict | None = None) -> dict:
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def write(rel_path: str, text: str) -> Path:
    path = TMP / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_cli(paths: list[Path], extra_args: list[str] | None = None, with_literal: bool = False) -> tuple[int, str]:
    cmd = [sys.executable, str(SCANNER)]
    for path in paths:
        cmd += ["--path", str(path)]
    if with_literal:
        cmd += ["--secret-env-var", RUN_LITERAL_ENV]
    cmd += extra_args or []
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(backend_dir.parent),
        env=_utf8_env(os.environ, {RUN_LITERAL_ENV: RUN_LITERAL}),
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


RUN_COUNT = 0


def step(name: str, ok: bool, detail: str = "") -> None:
    global RUN_COUNT
    RUN_COUNT += 1
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def summary_of(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("SECRET_SCAN_SUMMARY"):
            return line
    return "<无 SUMMARY 行>"


def find_line(output: str, prefix: str) -> str:
    for line in output.splitlines():
        if line.startswith(prefix):
            return line
    return ""


def expect_hit(name: str, rel_path: str, sample: str, kind: str) -> None:
    """单样本、单文件、默认排除规则下的命中断言。"""
    path = write(f"cases/{rel_path}", sample)
    code, out = run_cli([path])
    ok = code == 1 and f"kind={kind}" in out
    step(name, ok, f"exit={code} {summary_of(out)}")


def expect_clean(name: str, rel_path: str, sample: str) -> None:
    path = write(f"cases/{rel_path}", sample)
    code, out = run_cli([path])
    step(name, code == 0 and "findings=0" in out, f"exit={code} {summary_of(out)}")


# ---------------------------------------------------------------------------
# A. 正样本：赋值形状必须命中（逐条，含 R2 记录的三处盲区与本轮新发现的 JSON 引号键）
# ---------------------------------------------------------------------------
def positive_assignment_shapes() -> None:
    expect_hit("password: \"SuperSecret123\"（引号值，评审指定正样本）", "quoted.log",
               'password: "SuperSecret123"', "credential_assignment")
    expect_hit("api_key=abcdef123456（裸值）", "bare.log", "api_key=abcdef123456", "credential_assignment")
    expect_hit("{\"password\": \"...\"}（JSON 引号键，本轮新发现的第 4 处盲区）", "json.log",
               '{"password": "SuperSecret123"}', "credential_assignment")
    expect_hit("ADMIN_PASSWORD=...（下划线前缀键，盲区 2）", "env.log",
               "ADMIN_PASSWORD=RealSecret99", "credential_assignment")
    expect_hit("SECRET_KEY=...（下划线后缀键，盲区 2 变体）", "env2.log",
               "SECRET_KEY=django-insecure-abc123456789", "credential_assignment")
    expect_hit("password=ab;cdefghij（值含分号，盲区 1）", "semi.log",
               "password=ab;cdefghij", "credential_assignment")
    expect_hit("password={token:'...'}（值为对象字面量，盲区 3）", "obj.log",
               "password={token:'Realsecretvalue'}", "credential_assignment")
    expect_hit("括号不在调用位（ab;cd(efghij）仍然命中", "paren.log",
               "password=ab;cd(efghij", "credential_assignment")
    expect_hit("password_hash=<bcrypt>（bcrypt 形状）", "bcrypt.log",
               f"password_hash={BCRYPT_SAMPLE}", "bcrypt_hash")
    expect_hit("带口令 DSN", "dsn.log",
               "connect postgresql://appuser:s3cr3tpass@10.0.0.5:5432/graphinsight", "dsn_with_credentials")
    expect_hit("JWT", "jwt.log", f"authorization payload {JWT_SAMPLE}", "jwt")


# ---------------------------------------------------------------------------
# B. 负样本：结构性/占位值不得命中（放宽字符集后仍然安静）
# ---------------------------------------------------------------------------
def negative_structural() -> None:
    expect_clean("minified 结构赋值 o.password=null;const 不命中", "struct1.log",
                 "o.password=null;const t=1;")
    expect_clean("松散日志 password = null; 不命中", "struct2.log", "password = null;")
    expect_clean("this.password=null 不命中", "struct3.log", "this.password=null,")
    expect_clean("占位值 changeme 不命中", "ph1.log", "password=changeme")
    expect_clean("已声明 fixture 默认在占位表内不命中", "ph2.log", 'password: "graphinsight-dev-password"')
    expect_clean("非凭据键名 password_hint 不命中", "hint.log",
                 "password_hint=what_is_my_cat_name")
    expect_clean("驼峰标识符 passwordValidator 不命中", "camel.log",
                 "const passwordValidator = createValidator({})")
    expect_clean("非凭据后缀 password_reset 不命中", "reset.log", "password_reset=not-a-token-form")
    expect_clean("方法接收者 password=self.password 不命中", "py1.log", "password=self.password")
    expect_clean("调用表达式 password=os.getenv(\"ADMIN_PASSWORD\") 不命中", "py2.log",
                 'password=os.getenv("ADMIN_PASSWORD")')
    expect_clean("哈希函数 password=get_password_hash(raw) 不命中", "py3.log",
                 "password=get_password_hash(raw)")

    # 已知代价（不是遗漏）：值以"标识符紧跟左括号"开头时按调用表达式放过，
    # 因此真凭据若长成 Pa(ss)word 这种形态会漏检。这条断言把缺口钉在明面上，
    # 任何人想改变这个取舍都必须先动这条断言。
    expect_clean("【已知代价】值首段紧跟左括号的凭据不命中", "known_gap.log",
                 "password=Pa(ss)word12345")


# ---------------------------------------------------------------------------
# C. 字面值层与 fixture 放行
# ---------------------------------------------------------------------------
def literal_layer() -> None:
    lit_plain = write("cases/literal_plain.log", f"job payload token={RUN_LITERAL} done")
    code, out = run_cli([lit_plain], with_literal=True)
    step("注入凭据字面值命中 run_credential", code == 1 and "kind=run_credential" in out,
         f"exit={code} {summary_of(out)}")

    lit_bundled = write("artifacts/playwright-report/assets/app.min.js", f"var s={RUN_LITERAL};")
    code, out = run_cli([lit_bundled], with_literal=True)
    step("被排除的打包产物里，注入凭据字面值仍然命中（排除不削弱字面值层）",
         code == 1 and "kind=run_credential" in out and "kind=credential_assignment" not in out,
         f"exit={code} {summary_of(out)}")

    clean_lit = write("cases/literal_absent.log", "job payload done, no credential here")
    code, out = run_cli([clean_lit], with_literal=True)
    step("字面值未出现在产物里时保持放行", code == 0 and "findings=0" in out, f"exit={code}")

    # 必须用不在 PLACEHOLDER_VALUES 表里的值，否则"放行"是占位表给的，
    # 证不了 --allow-fixture 本身生效（本轮自查踩到的假通过）。
    fixture = write("cases/fixture.log", 'password: "ci-fixture-login-pw"')
    code, out = run_cli([fixture], extra_args=["--allow-fixture", "ci-fixture-login-pw"])
    step("--allow-fixture 显式放行已声明 fixture", code == 0 and "findings=0" in out, f"exit={code}")

    code, out = run_cli([fixture])
    step("同一 fixture 不加 --allow-fixture 时必须命中（证明上一条是放行而非漏检）",
         code == 1 and "kind=credential_assignment" in out, f"exit={code}")

    builtin = write("cases/fixture_builtin.log", 'password: "graphinsight-dev-password"')
    code, out = run_cli([builtin])
    step("内置占位表单独生效（无需 --allow-fixture）", code == 0 and "findings=0" in out, f"exit={code}")


# ---------------------------------------------------------------------------
# D. 路径排除层：只削弱赋值形状，且必须可审计
# ---------------------------------------------------------------------------
def exclusion_scope() -> None:
    js_min = write("artifacts/dist/assets/vendor.min.js", "o.password=e.value,console.log(1);")
    code, out = run_cli([js_min])
    step("默认排除：打包产物里的 JS 成员表达式不再误报",
         code == 0 and "findings=0" in out, f"exit={code} {summary_of(out)}")

    code, out = run_cli([js_min], extra_args=["--no-default-excludes"])
    step("负向自证：关掉默认排除后同一条必然命中（证明排除层在接误报，不是空规则）",
         code == 1 and "kind=credential_assignment" in out, f"exit={code} {summary_of(out)}")

    pw = write("artifacts/playwright-report/index.html", 'x.password="abc123456789";')
    code, out = run_cli([pw])
    step("默认排除：playwright-report/ 目录内赋值形状跳过",
         code == 0 and "findings=0" in out, f"exit={code}")

    custom = write("artifacts/onedump/snapshot/creds.json", 'password="abcdefgh12345"')
    code, out = run_cli([custom], extra_args=["--exclude", "*/onedump/*"])
    step("--exclude 自定义 glob 生效", code == 0 and "findings=0" in out, f"exit={code} {summary_of(out)}")

    code, out = run_cli([custom])
    step("同一样本不加 --exclude 时必须命中（证明上一条是范围而非漏检）",
         code == 1 and "kind=credential_assignment" in out, f"exit={code}")

    # 排除计数自洽 + 可审计
    code, out = run_cli([TMP / "artifacts"])
    line = summary_of(out)
    files = _int_after(line, "files=")
    scanned = _int_after(line, "shape_scanned_files=")
    excluded = _int_after(line, "shape_excluded_files=")
    step("SUMMARY 计数自洽（shape_scanned + shape_excluded == files）",
         files > 0 and scanned + excluded == files, line)
    step("存在排除时打印可审计 NOTE 与仍生效的层",
         excluded == 0
         or ("shape_assignment_skipped_files=" in out and "layers_still_scanned=" in out),
         find_line(out, "SECRET_SCAN_NOTE shape_assignment_skipped_files"))


def _int_after(line: str, key: str) -> int:
    for token in line.split():
        if token.startswith(key):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return -1
    return -1


# ---------------------------------------------------------------------------
# E. 输出与退出码契约（不得回显凭据明文）
# ---------------------------------------------------------------------------
def output_contract() -> None:
    secret = "SuperSecret123"
    path = write("cases/redaction.log", f'password: "{secret}"')
    code, out = run_cli([path])
    step("命中输出不回显凭据明文", code == 1 and secret not in out and "value=Supe" in out,
         out[:200])

    code, out = run_cli([])
    step("无 --path 时 exit 2 并给 PREREQ_MISSING", code == 2 and "SECRET_SCAN_PREREQ_MISSING" in out,
         f"exit={code}")

    code, out = run_cli([TMP / "no-such-dir"])
    step("路径不存在时 exit 2（fail-closed，不因缺目录静默放行）", code == 2, f"exit={code}")

    code, out = run_cli([TMP / "no-such-dir"], extra_args=["--skip-missing"])
    step("--skip-missing 时显式 SKIP 并 exit 0",
         code == 0 and "SECRET_SCAN_SKIPPED" in out, f"exit={code}")

    clean = write("cases/clean.log", "health check ok, nothing sensitive\n")
    code, out = run_cli([clean])
    step("干净产物 exit 0 且 result=pass", code == 0 and "result=pass" in out, f"exit={code}")

    step("扫描器源文件可编译", _compiles(), "")


def _compiles() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCANNER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(backend_dir.parent),
        env=_utf8_env(os.environ),
        timeout=120,
    )
    return proc.returncode == 0


def main() -> int:
    global TMP

    with tempfile.TemporaryDirectory(prefix="graphinsight-secret-selftest-") as raw:
        TMP = Path(raw)
        print(f"[fixture-root] {TMP}")
        positive_assignment_shapes()
        negative_structural()
        literal_layer()
        exclusion_scope()
        output_contract()

        print("-" * 60)
        for item in FAILURES:
            print(f"  FAILED: {item}")
        print(
            f"SECRET_SCAN_SELFTEST_SUMMARY assertions={RUN_COUNT} "
            f"failed={len(FAILURES)} result={'pass' if not FAILURES else 'fail'}"
        )
        return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
