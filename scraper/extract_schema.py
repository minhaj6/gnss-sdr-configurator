#!/usr/bin/env python3
"""Extract configuration->property(...) calls from a GNSS-SDR source tree
and emit schema/schema.json.

Parsing is done with tree-sitter and the tree-sitter-cpp grammar (decision
D5 in DECISIONS.md): every .cc file is parsed into a real C++ syntax tree,
and property calls, key expressions, default expressions, and local const
declarations are read off the tree by node type. No regex-based C++
scanning. The emitted shape is documented in schema/SCHEMA_SPEC.md (spec
version 1); keep the two in sync.

CLI:
    python scraper/extract_schema.py --gnss-sdr-root upstream/gnss-sdr \\
        --out schema/schema.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser, Query, QueryCursor

CPP = Language(tree_sitter_cpp.language())
PARSER = Parser(CPP)

# #if/#else/#endif interleaved with else-if chains (driver-gated signal
# sources in gnss_block_factory.cc) derail the C++ grammar, orphaning those
# branches. The schema must include every implementation regardless of
# build flags, so conditional directives are blanked before parsing.
# Blank lines keep line numbers stable.
PREPROC_CONDITIONAL_RE = re.compile(
    rb"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif)\b.*$", re.MULTILINE
)


def parse_cpp(source: bytes) -> Node:
    source = PREPROC_CONDITIONAL_RE.sub(b"", source)
    return PARSER.parse(source).root_node

# `R->property(...)` where R is one of these is a configuration lookup.
# `overrided_->property(...)` is deliberately excluded (internal delegation
# inside the ConfigurationInterface implementations).
RECEIVERS = {"configuration", "configuration_", "config_"}

# Any call of the form  <identifier> -> <field> ( <args> ) ; receiver and
# method name are filtered in Python rather than with query predicates.
PROPERTY_CALL_QUERY = Query(
    CPP,
    """
    (call_expression
      function: (field_expression
        argument: (identifier) @receiver
        field: (field_identifier) @method)
      arguments: (argument_list) @args) @call
    """,
)

CONSTRUCTOR_QUERY = Query(
    CPP,
    """
    (function_definition
      declarator: (function_declarator
        declarator: (qualified_identifier
          scope: (namespace_identifier) @scope
          name: (identifier) @name)))
    """,
)

DECLARATION_QUERY = Query(CPP, "(declaration) @decl")

# C++ type spelling -> schema type. Types not listed here are ignored when
# building the symbol table (their declarations can't hold our literals).
CPP_TYPE_TO_SCHEMA = {
    "bool": "bool",
    "float": "float",
    "double": "float",
    "std::string": "string",
    **{
        t: "int"
        for t in (
            "int8_t", "uint8_t", "int16_t", "uint16_t",
            "int32_t", "uint32_t", "int64_t", "uint64_t",
            "int", "long", "size_t", "std::size_t",
            "unsigned", "unsigned int", "unsigned long",
        )
    },
}


def text(node: Node) -> str:
    return node.text.decode("utf-8")


def classify_number(literal: str) -> tuple[str, object] | None:
    """Classify a C++ number_literal as int or float and parse its value."""
    is_hex = literal.lower().startswith("0x")
    # F is a hex digit, so only strip integer suffixes from hex literals
    stripped = literal.rstrip("uUlL") if is_hex else literal.rstrip("uUlLfF")
    try:
        if not is_hex and (
            "." in stripped
            or "e" in stripped.lower()
            or literal[-1] in "fF"
        ):
            return "float", float(stripped)
        return "int", int(stripped, 0)
    except ValueError:
        return None


def string_contents(node: Node) -> str:
    """Inner text of a string_literal / user_defined_literal ("..."s) /
    concatenated_string node, without the surrounding quotes."""
    if node.type == "user_defined_literal":
        inner = node.child(0)
        return string_contents(inner) if inner is not None else ""
    if node.type == "concatenated_string":
        return "".join(
            string_contents(c) for c in node.named_children
            if c.type in ("string_literal", "user_defined_literal")
        )
    # string_literal: children are '"', optional string_content, '"'
    return "".join(text(c) for c in node.children if c.type == "string_content")


def is_string_node(node: Node) -> bool:
    return node.type in (
        "string_literal", "user_defined_literal", "concatenated_string"
    )


def literal_from_node(node: Node) -> tuple[str, object] | None:
    """Resolve an expression node to (schema type, value) if it is a
    literal, possibly wrapped in a typed initializer or static_cast."""
    if node.type in ("true", "false"):
        return "bool", node.type == "true"
    if node.type == "number_literal":
        return classify_number(text(node))
    if is_string_node(node):
        return "string", string_contents(node)
    if node.type == "unary_expression":
        # e.g. -1, -0.5
        operand = node.child_by_field_name("argument")
        op = node.child_by_field_name("operator")
        if operand is None or op is None or text(op) not in "+-":
            return None
        inner = literal_from_node(operand)
        if inner is None or inner[0] not in ("int", "float"):
            return None
        value = -inner[1] if text(op) == "-" else inner[1]
        return inner[0], value
    if node.type == "compound_literal_expression":
        # uint64_t{0UL} — the declared type wins
        type_node = node.child_by_field_name("type")
        init = node.child_by_field_name("value")
        if type_node is None or init is None:
            return None
        schema_type = CPP_TYPE_TO_SCHEMA.get(text(type_node))
        if schema_type is None:
            return None
        literals = [literal_from_node(c) for c in init.named_children]
        if not literals:
            # value-initialization: T{} is the zero value of T
            zero = {"string": "", "int": 0, "float": 0.0, "bool": False}
            return schema_type, zero[schema_type]
        if len(literals) != 1 or literals[0] is None:
            return None
        return schema_type, literals[0][1]
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is None or args is None:
            return None
        arg_nodes = args.named_children
        fn_text = text(fn)
        if fn.type == "template_function" and fn_text.startswith("static_cast"):
            # static_cast<T>(literal) — the cast type wins
            targs = fn.child_by_field_name("arguments")
            if targs is None or len(targs.named_children) != 1:
                return None
            schema_type = CPP_TYPE_TO_SCHEMA.get(
                text(targs.named_children[0]).removeprefix("std::")
            )
            if schema_type is None or len(arg_nodes) != 1:
                return None
            inner = literal_from_node(arg_nodes[0])
            return None if inner is None else (schema_type, inner[1])
        if fn_text == "std::string":
            if not arg_nodes:
                return "string", ""
            if len(arg_nodes) == 1 and is_string_node(arg_nodes[0]):
                return "string", string_contents(arg_nodes[0])
            return None
        # functional-style cast on a known type: int64_t(0)
        schema_type = CPP_TYPE_TO_SCHEMA.get(fn_text)
        if schema_type is not None and len(arg_nodes) == 1:
            inner = literal_from_node(arg_nodes[0])
            return None if inner is None else (schema_type, inner[1])
        return None
    if node.type in ("parenthesized_expression", "initializer_list"):
        if len(node.named_children) == 1:
            return literal_from_node(node.named_children[0])
    return None


DEPRECATED_LHS_RE = re.compile(r"deprecat", re.IGNORECASE)

# gnss-sdr signal constants passed to block constructors: GPS_1C, GAL_5X,
# QZS_J1, ... — the part after the underscore is the channel signal code
# used in Channels_XX / Acquisition_XX role names.
SIGNAL_CONST_RE = re.compile(r"^(?:GPS|GAL|GLO|BDS|QZS|SBAS)_([A-Za-z0-9]+)$")

# some factory constants spell the band (GAL_E5a) rather than the channel
# signal code (5X) used in Channels_XX role names; normalize to the code
SIGNAL_CODE_ALIASES = {"E5a": "5X", "E5b": "7X"}

# fallback for factory branches that construct without a signal constant
# (telemetry decoders): infer the signal code from the implementation name.
# Ordered; first match wins.
NAME_SIGNAL_HINTS = (
    ("GPS_L1_CA", "1C"),
    ("GPS_L2", "2S"),
    ("GPS_L5", "L5"),
    ("Galileo_E1", "1B"),
    ("Galileo_E5a", "5X"),
    ("Galileo_E5b", "7X"),
    ("Galileo_E6", "E6"),
    ("GLONASS_L1", "1G"),
    ("GLONASS_L2", "2G"),
    ("BEIDOU_B1", "B1"),
    ("BEIDOU_B3", "B3"),
    ("QZSS_L1", "J1"),
    ("QZSS_L5", "J5"),
)


def string_comparisons(root: Node) -> dict[str, dict[str, None]]:
    """Map variable names (trailing underscores stripped) to the string
    literals they are compared against with == or != anywhere in the file.
    Used to offer value choices for enum-like parameters (item_type,
    filter_type, ...). Values keep source order (dict used as ordered set)."""
    comps: dict[str, dict[str, None]] = {}
    for node in walk_tree(root):
        if node.type != "binary_expression":
            continue
        op = node.child_by_field_name("operator")
        if op is None or text(op) not in ("==", "!="):
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        ident, lit = (left, right) if is_string_node(right) else (right, left)
        if not is_string_node(lit) or ident is None or ident.type != "identifier":
            continue
        literal = string_contents(lit)
        if literal:
            comps.setdefault(text(ident).rstrip("_"), {})[literal] = None
    return comps


def attach_options(entries: dict[str, dict], root: Node) -> None:
    """Attach an `options` list to entries whose parameter name is compared
    against string literals in the same file."""
    comps = string_comparisons(root)
    for entry in entries.values():
        tail = entry["key"].rsplit(".", 1)[-1]
        if "<" in tail or tail.endswith("implementation"):
            continue
        literals = comps.get(tail)
        if not literals:
            continue
        options = dict(literals)
        default = entry["default_value"]
        if entry["type"] == "string" and default:
            options.setdefault(default, None)
        entry["options"] = list(options)


def is_deprecated_use(call: Node) -> bool:
    """True when a property call's result is assigned to a variable whose
    name marks it deprecated (upstream convention: `fs_in_deprecated = ...`,
    `deprecation_warning = ...`)."""
    node = call
    for _ in range(4):  # allow a few wrapping levels (casts, parens)
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "init_declarator":
            name = parent.child_by_field_name("declarator")
            return name is not None and bool(
                DEPRECATED_LHS_RE.search(text(name))
            )
        if parent.type == "assignment_expression":
            left = parent.child_by_field_name("left")
            return left is not None and bool(
                DEPRECATED_LHS_RE.search(text(left))
            )
        node = parent
    return False


class UnresolvableKey(Exception):
    def __init__(self, part: str):
        super().__init__(part)
        self.part = part


def flatten_plus(node: Node) -> list[Node]:
    """Flatten a tree of `a + b + c` into its leaf operand nodes."""
    if node.type == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is None or text(op) != "+":
            raise UnresolvableKey(text(node))
        return flatten_plus(node.child_by_field_name("left")) + flatten_plus(
            node.child_by_field_name("right")
        )
    return [node]


def placeholder_name(identifier: str) -> str:
    return identifier.rstrip("_") or identifier


def normalize_key(node: Node) -> tuple[str, list[str]]:
    """Return (key pattern, placeholder names). Raises UnresolvableKey."""
    pieces: list[str] = []
    placeholders: list[str] = []
    for part in flatten_plus(node):
        if is_string_node(part):
            pieces.append(string_contents(part))
            continue
        if part.type == "identifier":
            name = placeholder_name(text(part))
        elif (
            part.type == "call_expression"
            and text(part.child_by_field_name("function")) == "std::to_string"
            and len(part.child_by_field_name("arguments").named_children) == 1
            and part.child_by_field_name("arguments").named_children[0].type
            == "identifier"
        ):
            name = placeholder_name(
                text(part.child_by_field_name("arguments").named_children[0])
            )
        else:
            raise UnresolvableKey(text(part))
        placeholders.append(name)
        pieces.append(f"<{name}>")
    return "".join(pieces), placeholders


def declared_schema_type(decl: Node) -> str | None:
    """Schema type of a `const <type> ...;` declaration, or None if the
    declaration is not const or its type is not one we track."""
    if not any(
        c.type == "type_qualifier" and text(c) == "const"
        for c in decl.children
    ):
        return None
    type_node = decl.child_by_field_name("type")
    return None if type_node is None else CPP_TYPE_TO_SCHEMA.get(text(type_node))


def build_symbol_table(root: Node) -> dict[str, tuple[str, object]]:
    """Map local `const <type> name = <literal>;` (or (…)/{…} initializer)
    declarations to (schema type, value). A default-constructed
    `const std::string name;` maps to the empty string."""
    table: dict[str, tuple[str, object]] = {}
    for match in QueryCursor(DECLARATION_QUERY).matches(root):
        decl = match[1]["decl"][0]
        schema_type = declared_schema_type(decl)
        if schema_type is None:
            continue
        for child in decl.named_children:
            if child.type == "identifier" and schema_type == "string":
                table.setdefault(text(child), ("string", ""))
            if child.type != "init_declarator":
                continue
            name_node = child.child_by_field_name("declarator")
            value_node = child.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier":
                continue
            if value_node is None:
                continue
            if value_node.type in ("argument_list", "initializer_list"):
                inner = value_node.named_children
                value_node = inner[0] if len(inner) == 1 else None
                if value_node is None:
                    if not inner and schema_type == "string":
                        table[text(name_node)] = ("string", "")
                    continue
            lit = literal_from_node(value_node)
            if lit is not None:
                table[text(name_node)] = (schema_type, lit[1])
    return table


def resolve_default(
    node: Node, symbols: dict[str, tuple[str, object]]
) -> tuple[str, object]:
    lit = literal_from_node(node)
    if lit is not None:
        return lit
    if node.type == "identifier" and text(node) in symbols:
        return symbols[text(node)]
    return "unknown", None


def find_property_calls(root: Node) -> list[tuple[Node, Node, Node]]:
    """Return (call, key_expr, default_expr) nodes for every well-formed
    two-argument property() call on a configuration receiver."""
    calls = []
    for match in QueryCursor(PROPERTY_CALL_QUERY).matches(root):
        captures = match[1]
        if text(captures["receiver"][0]) not in RECEIVERS:
            continue
        if text(captures["method"][0]) != "property":
            continue
        args = captures["args"][0].named_children
        if len(args) != 2:
            continue
        calls.append((captures["call"][0], args[0], args[1]))
    calls.sort(key=lambda c: c[0].start_byte)
    return calls


def adapter_name(root: Node, path: Path) -> str:
    """Class name owning this file's code: the `ClassName::ClassName(`
    constructor definition if present, else the most common scope among
    `ClassName::method(` definitions, else the CamelCased file stem."""
    scopes: list[str] = []
    for match in QueryCursor(CONSTRUCTOR_QUERY).matches(root):
        scope = text(match[1]["scope"][0])
        name = text(match[1]["name"][0])
        if scope == name:
            return name
        scopes.append(scope)
    if scopes:
        return max(set(scopes), key=scopes.count)
    return "".join(w.capitalize() for w in path.stem.split("_"))


def squash_ws(s: str) -> str:
    return " ".join(s.split())


def extract_file(
    path: Path, root_dir: Path
) -> tuple[list[dict], list[dict], int, str]:
    """Return (entries, unresolved, calls_matched, adapter) for one .cc
    file."""
    source = path.read_bytes()
    root = parse_cpp(source)
    rel = str(path.relative_to(root_dir))
    symbols = build_symbol_table(root)
    adapter = adapter_name(root, path)
    entries: dict[str, dict] = {}
    unresolved: list[dict] = []
    calls = find_property_calls(root)
    for call, key_node, default_node in calls:
        line = call.start_point[0] + 1
        default_expr = squash_ws(text(default_node))
        try:
            key, placeholders = normalize_key(key_node)
        except UnresolvableKey as exc:
            unresolved.append(
                {
                    "file": rel,
                    "line": line,
                    "key_expr": squash_ws(text(key_node)),
                    "default_expr": default_expr,
                    "reason": (
                        f"key part {squash_ws(exc.part)!r} is not a "
                        "resolvable form"
                    ),
                }
            )
            continue
        deprecated = is_deprecated_use(call)
        if key in entries:
            entry = entries[key]
            entry["occurrences"] += 1
            # any non-deprecated use means the key is live
            entry["deprecated"] = entry["deprecated"] and deprecated
            if default_expr != entry["default_expr"]:
                others = entry.setdefault("other_default_exprs", [])
                if default_expr not in others:
                    others.append(default_expr)
            continue
        schema_type, value = resolve_default(default_node, symbols)
        entries[key] = {
            "key": key,
            "placeholders": placeholders,
            "type": schema_type,
            "default_expr": default_expr,
            "default_value": value,
            "adapter": adapter,
            "file": rel,
            "line": line,
            "occurrences": 1,
            "deprecated": deprecated,
        }
    attach_options(entries, root)
    for entry in entries.values():
        if not entry["deprecated"]:
            del entry["deprecated"]  # optional field: present only when true
    return list(entries.values()), unresolved, len(calls), adapter


def parse_literal(expr: str) -> tuple[str, object] | None:
    """Parse a C++ literal given as text (test/debug helper): wraps the
    expression in a snippet, parses it, and delegates to literal_from_node."""
    snippet = f"auto x = {expr};".encode()
    root = PARSER.parse(snippet).root_node
    decls = [n for n in root.named_children if n.type == "declaration"]
    if len(decls) != 1 or root.has_error:
        return None
    for child in decls[0].named_children:
        if child.type == "init_declarator":
            value = child.child_by_field_name("value")
            if value is not None:
                return literal_from_node(value)
    return None


def walk_tree(node: Node):
    yield node
    for child in node.children:
        yield from walk_tree(child)


def function_return_types(root: Node) -> dict[str, str]:
    """Map function names defined in this file to their class return types
    (e.g. get_tlm_conf -> Tlm_Conf)."""
    types: dict[str, str] = {}
    for node in walk_tree(root):
        if node.type != "function_definition":
            continue
        rtype = node.child_by_field_name("type")
        decl = node.child_by_field_name("declarator")
        if rtype is None or rtype.type != "type_identifier":
            continue
        while decl is not None and decl.type != "function_declarator":
            decl = decl.child_by_field_name("declarator")
        if decl is None:
            continue
        name = decl.child_by_field_name("declarator")
        if name is not None and name.type == "identifier":
            types[text(name)] = text(rtype)
    return types


def extract_implementations(root: Node) -> list[dict]:
    """Map user-facing implementation strings to the class whose parameters
    apply, by reading gnss_block_factory.cc's `if (implementation ==
    "Name")` chains: the branch's `std::make_unique<AdapterClass>` if
    present, else the return type of a factory-local helper call in the
    branch (e.g. get_tlm_conf(...) -> Tlm_Conf)."""
    impls: dict[str, str] = {}
    ret_types = function_return_types(root)
    for node in walk_tree(root):
        if node.type != "binary_expression":
            continue
        op = node.child_by_field_name("operator")
        if op is None or text(op) not in ("==", "!="):
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        ident, lit = (left, right) if is_string_node(right) else (right, left)
        if not is_string_node(lit) or ident.type != "identifier":
            continue
        if text(ident) not in ("implementation", "signal_conditioner"):
            continue
        name = string_contents(lit)
        # enclosing if-statement
        enclosing = node
        while enclosing is not None and enclosing.type != "if_statement":
            enclosing = enclosing.parent
        if enclosing is None:
            continue
        if text(op) == "==":
            consequence = enclosing.child_by_field_name("consequence")
            search_nodes = [] if consequence is None else [consequence]
        else:
            # guard clause: `if (x != "Name") { error }` — the construction
            # for "Name" follows the if-statement in the enclosing scope
            parent = enclosing.parent
            if parent is None:
                continue
            siblings = parent.children
            search_nodes = siblings[siblings.index(enclosing) + 1 :]
        adapter = None
        signal = None
        for n in (x for s in search_nodes for x in walk_tree(s)):
            if n.type == "template_function" and text(n.child(0)) == "make_unique":
                targs = n.child_by_field_name("arguments")
                if targs is not None and targs.named_children:
                    adapter = text(targs.named_children[0])
                    signal = _signal_from_call(n)
                    break
        if adapter is None:
            for n in (x for s in search_nodes for x in walk_tree(s)):
                if n.type != "call_expression":
                    continue
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "identifier" and text(fn) in ret_types:
                    adapter = ret_types[text(fn)]
                    break
        if adapter is not None:
            if signal is None:
                signal = next(
                    (code for hint, code in NAME_SIGNAL_HINTS if hint in name),
                    None,
                )
            impls.setdefault(name, (adapter, signal))
    result = []
    for name, (adapter, signal) in sorted(impls.items()):
        impl = {"name": name, "adapter": adapter}
        if signal is not None:
            impl["signal"] = signal
        result.append(impl)
    return result


def _signal_from_call(make_unique_fn: Node) -> str | None:
    """Signal code from a constructor call's signal-constant argument
    (e.g. make_unique<PcpsAcquisitionAdapter>(..., GPS_1C) -> '1C')."""
    node = make_unique_fn
    while node is not None and node.type != "call_expression":
        node = node.parent
    if node is None:
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        if arg.type == "identifier":
            m = SIGNAL_CONST_RE.match(text(arg))
            if m:
                code = m.group(1)
                return SIGNAL_CODE_ALIASES.get(code, code)
    return None


def extract_base_classes(root: Node) -> dict[str, list[str]]:
    """Map each class defined in this file to its base classes."""
    bases: dict[str, list[str]] = {}
    for node in walk_tree(root):
        if node.type != "class_specifier":
            continue
        name = node.child_by_field_name("name")
        clause = next(
            (c for c in node.children if c.type == "base_class_clause"), None
        )
        if name is None or clause is None:
            continue
        bases[text(name)] = [
            text(c)
            for c in clause.children
            if c.type in ("type_identifier", "qualified_identifier")
        ]
    return bases


def target_files(root: Path) -> list[Path]:
    files = sorted(root.glob("src/algorithms/*/adapters/*.cc"))
    # helper classes like Acq_Conf / Dll_Pll_Conf / Pass_Through read many
    # properties on the adapters' behalf
    files += sorted(root.glob("src/algorithms/*/libs/*.cc"))
    files += sorted(root.glob("src/algorithms/libs/*.cc"))
    files += sorted(root.glob("src/core/receiver/*.cc"))
    return files


def header_files(root: Path) -> list[Path]:
    files = sorted(root.glob("src/algorithms/*/adapters/*.h"))
    files += sorted(root.glob("src/algorithms/*/libs/*.h"))
    files += sorted(root.glob("src/algorithms/libs/*.h"))
    return files


def extract_member_types(root: Node) -> dict[str, list[str]]:
    """Map each class defined in a header to the types of its data
    members (used to link adapters to the helper classes they configure
    through, e.g. PcpsAcquisitionAdapter -> Acq_Conf)."""
    members: dict[str, list[str]] = {}
    for node in walk_tree(root):
        if node.type != "class_specifier":
            continue
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        types = []
        for child in walk_tree(body):
            if child.type != "field_declaration":
                continue
            type_node = child.child_by_field_name("type")
            if type_node is not None and type_node.type == "type_identifier":
                types.append(text(type_node))
        if types:
            members.setdefault(text(name), []).extend(types)
    return members


def git_commit(root: Path) -> str:
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha + ("-dirty" if dirty else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gnss-sdr-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--generated-from",
        help="override the git commit recorded in the header (use when the "
        "root is not a git checkout)",
    )
    args = parser.parse_args()

    root = args.gnss_sdr_root.resolve()
    if not (root / "src").is_dir():
        sys.exit(f"error: {root} does not look like a GNSS-SDR source tree")

    if args.generated_from:
        commit = args.generated_from
    else:
        try:
            commit = git_commit(root)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            sys.exit(
                f"error: could not read git commit from {root} ({exc}); "
                "pass --generated-from explicitly"
            )

    files = target_files(root)
    if not files:
        sys.exit(f"error: no target .cc files found under {root}")

    all_entries: list[dict] = []
    all_unresolved: list[dict] = []
    adapter_files: dict[str, str] = {}
    calls_matched = 0
    for path in files:
        entries, unresolved, calls, adapter = extract_file(path, root)
        all_entries.extend(entries)
        all_unresolved.extend(unresolved)
        calls_matched += calls
        adapter_files.setdefault(adapter, str(path.relative_to(root)))
    adapter_files = dict(sorted(adapter_files.items()))

    all_entries.sort(key=lambda e: (e["file"], e["line"], e["key"]))
    all_unresolved.sort(key=lambda u: (u["file"], u["line"]))

    factory = root / "src" / "core" / "receiver" / "gnss_block_factory.cc"
    if not factory.is_file():
        sys.exit(f"error: {factory} not found; cannot map implementations")
    implementations = extract_implementations(parse_cpp(factory.read_bytes()))

    adapters_with_entries = {e["adapter"] for e in all_entries}
    adapter_bases: dict[str, list[str]] = {}
    adapter_uses: dict[str, list[str]] = {}
    for path in header_files(root):
        header_root = parse_cpp(path.read_bytes())
        for cls, bases in extract_base_classes(header_root).items():
            kept = [b for b in bases if b in adapters_with_entries]
            if kept:
                adapter_bases[cls] = kept
        for cls, member_types in extract_member_types(header_root).items():
            kept = sorted(
                {t for t in member_types if t in adapters_with_entries and t != cls}
            )
            if kept:
                adapter_uses[cls] = kept
    adapter_bases = dict(sorted(adapter_bases.items()))
    adapter_uses = dict(sorted(adapter_uses.items()))

    doc = {
        "generated_from": commit,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "counts": {
            "files_scanned": len(files),
            "property_calls_matched": calls_matched,
            "entries": len(all_entries),
            "unresolved": len(all_unresolved),
            "implementations": len(implementations),
        },
        "implementations": implementations,
        "adapter_bases": adapter_bases,
        "adapter_uses": adapter_uses,
        "adapter_files": adapter_files,
        "entries": all_entries,
        "unresolved": all_unresolved,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {args.out}: {len(all_entries)} entries, "
        f"{len(all_unresolved)} unresolved, from {len(files)} files "
        f"({calls_matched} calls matched)"
    )


if __name__ == "__main__":
    main()
