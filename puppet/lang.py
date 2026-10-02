"""词法与语法：把源文本行解析为语句与表达式（`spec/01-grammar.md`）。

行是语句的最小单位；裸换行不续行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .diag import Diagnostic, ERROR

# ------------------------------------------------------------------ 表达式

class Expr:
    pass


@dataclass
class Lit(Expr):
    value: Any
    raw: str = ""


@dataclass
class Ref(Expr):
    addr: str                       # 不含 '#'
    field: Optional[str] = None


@dataclass
class Local(Expr):
    name: str
    field: Optional[str] = None


@dataclass
class Bin(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass
class Un(Expr):
    op: str
    operand: Expr


@dataclass
class Call(Expr):
    name: str
    args: List[Expr] = field(default_factory=list)


@dataclass
class ListLit(Expr):
    items: List[Expr] = field(default_factory=list)


@dataclass
class DictLit(Expr):
    fields: Dict[str, Expr] = field(default_factory=dict)


def is_static(expr: Expr) -> bool:
    """纯字面量（含由字面量构成的列表/字典）——不构成绑定。"""
    if isinstance(expr, Lit):
        return True
    if isinstance(expr, ListLit):
        return all(is_static(i) for i in expr.items)
    if isinstance(expr, DictLit):
        return all(is_static(v) for v in expr.fields.values())
    return False


def collect_refs(expr: Expr, out=None) -> List[Ref]:
    out = [] if out is None else out
    if isinstance(expr, Ref):
        out.append(expr)
    elif isinstance(expr, (Bin,)):
        collect_refs(expr.left, out)
        collect_refs(expr.right, out)
    elif isinstance(expr, Un):
        collect_refs(expr.operand, out)
    elif isinstance(expr, Call):
        for a in expr.args:
            collect_refs(a, out)
    elif isinstance(expr, ListLit):
        for a in expr.items:
            collect_refs(a, out)
    elif isinstance(expr, DictLit):
        for a in expr.fields.values():
            collect_refs(a, out)
    return out


def expr_source(expr: Expr) -> str:
    """把表达式还原为规范源文本（用于状态/程序写回与诊断）。"""
    if isinstance(expr, Lit):
        if isinstance(expr.value, bool):
            return "true" if expr.value else "false"
        if isinstance(expr.value, str):
            return '"%s"' % expr.value.replace('"', '\\"')
        return str(expr.value)
    if isinstance(expr, Ref):
        return "#" + expr.addr + ("." + expr.field if expr.field else "")
    if isinstance(expr, Local):
        return expr.name + ("." + expr.field if expr.field else "")
    if isinstance(expr, Bin):
        return "%s %s %s" % (expr_source(expr.left), expr.op, expr_source(expr.right))
    if isinstance(expr, Un):
        return "%s %s" % (expr.op, expr_source(expr.operand))
    if isinstance(expr, Call):
        return "%s(%s)" % (expr.name, ", ".join(expr_source(a) for a in expr.args))
    if isinstance(expr, ListLit):
        return "[" + ", ".join(expr_source(i) for i in expr.items) + "]"
    if isinstance(expr, DictLit):
        return "{" + ", ".join("%s: %s" % (k, expr_source(v)) for k, v in expr.fields.items()) + "}"
    return ""


# ------------------------------------------------------------------ 语句

@dataclass
class Stmt:
    line: int = 0


@dataclass
class Add(Stmt):
    parent: str = ""
    type: str = ""
    self_id: str = ""
    attrs: Dict[str, Expr] = field(default_factory=dict)
    as_name: str = ""
    anchor: Optional[tuple] = None          # ("before"|"after", addr)
    is_upsert: bool = False


@dataclass
class SetStmt(Stmt):
    target: str = ""
    attrs: Dict[str, Expr] = field(default_factory=dict)


@dataclass
class Del(Stmt):
    target: str = ""


@dataclass
class Move(Stmt):
    target: str = ""
    new_parent: str = ""
    anchor: Optional[tuple] = None


@dataclass
class Data(Stmt):
    source: str = ""
    persist: bool = False
    initial: Optional[Expr] = None
    schema: Dict[str, tuple] = field(default_factory=dict)   # 字段 -> (类型, 默认表达式|None)


@dataclass
class Action:
    kind: str = ""
    target: str = ""
    attrs: Dict[str, Expr] = field(default_factory=dict)
    item: Optional[Expr] = None
    bind: str = ""
    where: Optional[Expr] = None
    by: str = ""
    func: str = ""
    params: Dict[str, Expr] = field(default_factory=dict)
    into: str = ""


@dataclass
class On(Stmt):
    target: str = ""
    event: str = ""
    bind: str = ""
    when: Optional[Expr] = None
    actions: List[Action] = field(default_factory=list)


@dataclass
class Listen(Stmt):
    target: str = ""
    event: str = ""


@dataclass
class CallStmt(Stmt):
    func: str = ""
    params: Dict[str, Expr] = field(default_factory=dict)
    into: str = ""


@dataclass
class Probe(Stmt):
    verb: str = ""
    target: str = ""


@dataclass
class ActionStmt(Stmt):
    """顶层动作：集合原语也可以直接作为语句驱动数据。"""

    action: Optional[Action] = None


# ------------------------------------------------------------------ 词法

_PUNCT = set("=,:;{}[]().")
_OPS2 = ("==", "!=", "<=", ">=")
_OPS1 = ("+", "-", "*", "/", "<", ">")
_HEX = set("0123456789abcdefABCDEF")
_ID0 = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDN = _ID0 | set("0123456789")


@dataclass
class Tok:
    kind: str
    text: str
    value: Any = None
    pos: int = 0


class ParseError(Exception):
    def __init__(self, message, pos=0):
        super().__init__(message)
        self.message = message
        self.pos = pos


def tokenize(line: str) -> List[Tok]:
    toks: List[Tok] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "/" and line[i:i + 2] == "//":
            break          # 注释到行尾（规范 01 第 2 节）；`//` 无其他含义，故与地址无歧义
        if ch == "#":
            j = i + 1
            while j < n and line[j] in _IDN:
                j += 1
            word = line[i + 1:j]
            if not word:
                raise ParseError("井号后需要地址", i)
            # 只认 6/8 位：3 位会与 `#bad` / `#dad` / `#abc` 这类短地址撞车
            if all(c in _HEX for c in word) and len(word) in (6, 8):
                toks.append(Tok("COLOR", "#" + word, "#" + word, i))
            else:
                toks.append(Tok("ADDR", word, word, i))
            i = j
            continue
        if ch == '"':
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ParseError("字符串未闭合", i)
                if line[j] == "\\" and j + 1 < n:
                    buf.append(line[j + 1])
                    j += 2
                    continue
                if line[j] == '"':
                    break
                buf.append(line[j])
                j += 1
            toks.append(Tok("STRING", line[i:j + 1], "".join(buf), i))
            i = j + 1
            continue
        if ch.isdigit():
            j = i
            while j < n and line[j].isdigit():
                j += 1
            isf = False
            if j < n and line[j] == "." and j + 1 < n and line[j + 1].isdigit():
                isf = True
                j += 1
                while j < n and line[j].isdigit():
                    j += 1
            text = line[i:j]
            toks.append(Tok("NUMBER", text, float(text) if isf else int(text), i))
            i = j
            continue
        if ch in _ID0:
            j = i
            while j < n and line[j] in _IDN:
                j += 1
            toks.append(Tok("IDENT", line[i:j], line[i:j], i))
            i = j
            continue
        two = line[i:i + 2]
        if two in _OPS2:
            toks.append(Tok("OP", two, two, i))
            i += 2
            continue
        if ch in _OPS1:
            toks.append(Tok("OP", ch, ch, i))
            i += 1
            continue
        if ch in _PUNCT:
            toks.append(Tok("PUNCT", ch, ch, i))
            i += 1
            continue
        raise ParseError("无法识别的字符 %r" % ch, i)
    toks.append(Tok("END", "", None, n))
    return toks


# ------------------------------------------------------------------ 语法

class Parser:
    def __init__(self, toks: List[Tok], line: int):
        self.toks = toks
        self.i = 0
        self.line = line

    # -- 基础
    def peek(self, k: int = 0) -> Tok:
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def next(self) -> Tok:
        tok = self.peek()
        if tok.kind != "END":
            self.i += 1
        return tok

    def at(self, kind: str, text: Optional[str] = None) -> bool:
        tok = self.peek()
        return tok.kind == kind and (text is None or tok.text == text)

    def accept(self, kind: str, text: Optional[str] = None) -> Optional[Tok]:
        if self.at(kind, text):
            return self.next()
        return None

    def expect(self, kind: str, text: Optional[str] = None) -> Tok:
        if not self.at(kind, text):
            tok = self.peek()
            raise ParseError("期望 %s%s，实际 %r" % (kind, "/" + text if text else "", tok.text), tok.pos)
        return self.next()

    def expect_addr(self) -> str:
        return self.expect("ADDR").text

    def done(self) -> bool:
        return self.peek().kind == "END"

    # -- 表达式
    def expr(self) -> Expr:
        return self.or_expr()

    def or_expr(self) -> Expr:
        node = self.and_expr()
        while self.at("IDENT", "or"):
            self.next()
            node = Bin("or", node, self.and_expr())
        return node

    def and_expr(self) -> Expr:
        node = self.not_expr()
        while self.at("IDENT", "and"):
            self.next()
            node = Bin("and", node, self.not_expr())
        return node

    def not_expr(self) -> Expr:
        if self.at("IDENT", "not"):
            self.next()
            return Un("not", self.not_expr())
        return self.cmp_expr()

    def cmp_expr(self) -> Expr:
        node = self.add_expr()
        while self.at("OP") and self.peek().text in ("==", "!=", "<", "<=", ">", ">="):
            op = self.next().text
            node = Bin(op, node, self.add_expr())
        return node

    def add_expr(self) -> Expr:
        node = self.mul_expr()
        while self.at("OP") and self.peek().text in ("+", "-"):
            op = self.next().text
            node = Bin(op, node, self.mul_expr())
        return node

    def mul_expr(self) -> Expr:
        node = self.unary()
        while self.at("OP") and self.peek().text in ("*", "/"):
            op = self.next().text
            node = Bin(op, node, self.unary())
        return node

    def unary(self) -> Expr:
        if self.at("OP", "-"):
            self.next()
            return Un("-", self.unary())
        return self.primary()

    def primary(self) -> Expr:
        tok = self.peek()
        if tok.kind == "NUMBER":
            self.next()
            return Lit(tok.value, tok.text)
        if tok.kind == "STRING":
            self.next()
            return Lit(tok.value, tok.text)
        if tok.kind == "COLOR":
            self.next()
            return Lit(tok.value, tok.text)
        if tok.kind == "ADDR":
            self.next()
            field = None
            if self.accept("PUNCT", "."):
                field = self.expect("IDENT").text
            return Ref(tok.text, field)
        if tok.kind == "PUNCT" and tok.text == "(":
            self.next()
            inner = self.expr()
            self.expect("PUNCT", ")")
            return inner
        if tok.kind == "PUNCT" and tok.text == "[":
            self.next()
            items: List[Expr] = []
            if not self.at("PUNCT", "]"):
                items.append(self.expr())
                while self.accept("PUNCT", ","):
                    items.append(self.expr())
            self.expect("PUNCT", "]")
            return ListLit(items)
        if tok.kind == "PUNCT" and tok.text == "{":
            return self.dict_lit()
        if tok.kind == "IDENT":
            if tok.text == "true":
                self.next()
                return Lit(True, "true")
            if tok.text == "false":
                self.next()
                return Lit(False, "false")
            if self.peek(1).kind == "PUNCT" and self.peek(1).text == "(":
                self.next()
                self.next()
                args: List[Expr] = []
                if not self.at("PUNCT", ")"):
                    args.append(self.expr())
                    while self.accept("PUNCT", ","):
                        args.append(self.expr())
                self.expect("PUNCT", ")")
                return Call(tok.text, args)
            if self.peek(1).kind == "PUNCT" and self.peek(1).text == ".":
                self.next()
                self.next()
                return Local(tok.text, self.expect("IDENT").text)
            self.next()
            return Lit(tok.text, tok.text)
        raise ParseError("无法解析的表达式起点 %r" % tok.text, tok.pos)

    def dict_lit(self) -> DictLit:
        self.expect("PUNCT", "{")
        fields: Dict[str, Expr] = {}
        if not self.at("PUNCT", "}"):
            while True:
                key = self.next()
                if key.kind not in ("IDENT", "STRING"):
                    raise ParseError("字典键必须是名字或字符串", key.pos)
                self.expect("PUNCT", ":")
                fields[key.value if key.kind == "STRING" else key.text] = self.expr()
                if not self.accept("PUNCT", ","):
                    break
        self.expect("PUNCT", "}")
        return DictLit(fields)

    # -- 属性
    def attr_list(self, stop_words=()) -> Dict[str, Expr]:
        attrs: Dict[str, Expr] = {}
        while self.at("IDENT") and self.peek().text not in stop_words:
            if not (self.peek(1).kind == "PUNCT" and self.peek(1).text == "="):
                break
            key = self.next().text
            self.next()                      # '='
            attrs[key] = self.expr()
        return attrs

    # -- 语句
    def statement(self) -> Optional[Stmt]:
        if self.done():
            return None
        head = self.expect("IDENT")
        verb = head.text
        if verb == "add":
            return self.add_stmt(is_upsert=False)
        if verb == "upsert":
            return self.add_stmt(is_upsert=True)
        if verb == "set":
            st = SetStmt(line=self.line)
            st.target = self.expect_addr()
            st.attrs = self.attr_list()
            return st
        if verb == "del":
            st = Del(line=self.line)
            st.target = self.expect_addr()
            return st
        if verb == "move":
            st = Move(line=self.line)
            st.target = self.expect_addr()
            st.new_parent = self.expect_addr()
            st.anchor = self.anchor()
            return st
        if verb == "data":
            return self.data_stmt()
        if verb == "on":
            return self.on_stmt()
        if verb == "listen":
            st = Listen(line=self.line)
            st.target = self.expect_addr()
            st.event = self.expect("IDENT").text
            return st
        if verb == "call":
            return self.call_stmt()
        if verb in ("append", "remove", "remove_where", "update_where", "clear", "sort"):
            st = ActionStmt(line=self.line)
            self.i -= 1                     # 把动词交还给动作解析器
            st.action = self.action()
            return st
        if verb in ("tree", "get", "where"):
            st = Probe(line=self.line, verb=verb)
            if self.at("ADDR"):
                st.target = self.next().text
            return st
        raise ParseError("未知动词 %r" % verb, head.pos)

    def anchor(self) -> Optional[tuple]:
        if self.at("IDENT", "before") or self.at("IDENT", "after"):
            word = self.next().text
            return (word, self.expect_addr())
        return None

    def add_stmt(self, is_upsert: bool) -> Add:
        st = Add(line=self.line, is_upsert=is_upsert)
        st.parent = self.expect_addr()
        st.type = self.expect("IDENT").text
        st.self_id = self.expect_addr()
        if self.at("IDENT", "as"):
            self.next()
            st.as_name = self.expect("IDENT").text
        st.attrs = self.attr_list(stop_words=("before", "after", "as"))
        st.anchor = self.anchor()
        if self.at("IDENT", "as"):
            self.next()
            st.as_name = self.expect("IDENT").text
        return st

    def data_stmt(self) -> Data:
        st = Data(line=self.line)
        st.source = self.expect_addr()
        if self.at("IDENT", "persist"):
            self.next()
            self.expect("PUNCT", "=")
            tok = self.expect("IDENT")
            st.persist = tok.text == "true"
        self.expect("PUNCT", "=")
        st.initial = self.expr()
        if self.at("IDENT", "of"):
            self.next()
            st.schema = self.schema()
        return st

    def schema(self) -> Dict[str, tuple]:
        self.expect("PUNCT", "{")
        out: Dict[str, tuple] = {}
        if not self.at("PUNCT", "}"):
            while True:
                name = self.expect("IDENT").text
                self.expect("PUNCT", ":")
                typ = self.expect("IDENT").text
                default = None
                if self.accept("PUNCT", "="):
                    default = self.expr()
                out[name] = (typ, default)
                if not self.accept("PUNCT", ","):
                    break
        self.expect("PUNCT", "}")
        return out

    def on_stmt(self) -> On:
        st = On(line=self.line)
        st.target = self.expect_addr()
        st.event = self.expect("IDENT").text
        if self.at("IDENT", "as"):
            self.next()
            st.bind = self.expect("IDENT").text
        if self.at("IDENT", "when"):
            self.next()
            st.when = self.expr()
        self.expect("PUNCT", ":")
        st.actions = self.action_list()
        return st

    def action_list(self) -> List[Action]:
        groups: List[List[Tok]] = [[]]
        depth = 0
        while not self.done():
            tok = self.next()
            if tok.kind == "PUNCT" and tok.text in "([{":
                depth += 1
            elif tok.kind == "PUNCT" and tok.text in ")]}":
                depth -= 1
            if tok.kind == "PUNCT" and tok.text == ";" and depth == 0:
                groups.append([])
                continue
            groups[-1].append(tok)
        actions = []
        for group in groups:
            if not group:
                continue
            sub = Parser(group + [Tok("END", "", None, 0)], self.line)
            actions.append(sub.action())
        return actions

    def action(self) -> Action:
        head = self.expect("IDENT")
        kind = head.text
        act = Action(kind=kind)
        if kind == "set":
            act.target = self.expect_addr()
            act.attrs = self.attr_list()
        elif kind == "append":
            act.target = self.expect_addr()
            act.item = self.named_expr("item")
        elif kind == "remove":
            act.target = self.expect_addr()
            act.item = self.named_expr("item")
        elif kind == "remove_where":
            act.target = self.expect_addr()
            self.expect("IDENT", "as")
            act.bind = self.expect("IDENT").text
            self.expect("IDENT", "where")
            act.where = self.expr()
        elif kind == "update_where":
            act.target = self.expect_addr()
            self.expect("IDENT", "as")
            act.bind = self.expect("IDENT").text
            self.expect("IDENT", "set")
            act.attrs = self.attr_list(stop_words=("where",))
            self.expect("IDENT", "where")
            act.where = self.expr()
        elif kind == "clear":
            act.target = self.expect_addr()
        elif kind == "sort":
            act.target = self.expect_addr()
            self.expect("IDENT", "by")
            act.by = self.expect("IDENT").text
        elif kind == "call":
            act.func = self.next().text
            self.expect("IDENT", "with")
            act.params = self.dict_lit().fields
            self.expect("IDENT", "into")
            act.into = self.expect_addr()
        else:
            raise ParseError("未知动作 %r" % kind, head.pos)
        return act

    def named_expr(self, name: str) -> Expr:
        self.expect("IDENT", name)
        self.expect("PUNCT", "=")
        return self.expr()

    def call_stmt(self) -> CallStmt:
        st = CallStmt(line=self.line)
        st.func = self.next().text
        self.expect("IDENT", "with")
        st.params = self.dict_lit().fields
        self.expect("IDENT", "into")
        st.into = self.expect_addr()
        return st


def _is_comment(line: str) -> bool:
    return line.startswith("//")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def parse_line(text: str, line_no: int = 0) -> Optional[Stmt]:
    """解析一行；注释与空行返回 None。"""
    stripped = text.strip()
    if not stripped or _is_comment(stripped):
        return None
    parser = Parser(tokenize(stripped), line_no)
    stmt = parser.statement()
    if not parser.done():
        tok = parser.peek()
        raise ParseError("语句后有多余记号 %r" % tok.text, tok.pos)
    if stmt is not None:
        stmt.line = line_no
    return stmt


def parse_program(lines: List[str]):
    """返回 (语句列表, 诊断列表)。语法错误不阻断后续行。

    源文本形状（规范 01 第 1 节）：一条语句默认写在一行内；
    **唯一例外是行为头**——以 `on … :` 结尾的行，其动作体可以写在
    紧随其后、缩进更深的多行上；遇到第一行缩进不深于该行为头即结束。
    空行与注释行被忽略，不参与缩进判定。
    """
    stmts: List[Stmt] = []
    diags: List[Diagnostic] = []
    total = len(lines)
    index = 0
    while index < total:
        raw = lines[index]
        line_no = index + 1
        index += 1
        stripped = raw.strip()
        if not stripped or _is_comment(stripped):
            continue

        body: List[str] = []
        if stripped.startswith("on ") and stripped.endswith(":"):
            base = _indent(raw)
            while index < total:
                nxt = lines[index]
                s = nxt.strip()
                if not s or _is_comment(s):
                    index += 1
                    continue
                if _indent(nxt) <= base:
                    break
                body.append(s)
                index += 1

        text = stripped if not body else stripped + " " + " ; ".join(body)
        try:
            stmt = parse_line(text, line_no)
        except ParseError as ex:
            diags.append(Diagnostic("SYNTAX", ERROR, str(ex.message),
                                    line=line_no, col=ex.pos + 1))
            continue
        if stmt is not None:
            stmts.append(stmt)
    return stmts, diags
