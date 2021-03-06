import re

_annotations = {
    "short": "int",
    "int": "int",
    "uint8_t": "int",
    "uint16_t": "int",
    "int32_t": "int",
    "uint32_t": "int",
    "size_t": "int",
    "double": "float",
    "char": "str",
    "bool": "bool",
    "ctre::phoenix::motorcontrol::ControlMode": "ControlMode",
    "ctre::phoenix::ErrorCode": "ErrorCode",
    "void": "None",
}

# fmt: off

def _gen_check(pname, ptype, strict=False):
    SIGNED_CHECK = "isinstance({0}, int) and -1<<{1} <= {0} < 1<<{1}".format
    UNSIGNED_CHECK = "isinstance({0}, int) and 0 <= {0} < 1<<{1}".format

    SIGNED_SIZES = {
        # "byte": 8,
        # "int8_t": 8,
        "short": 16,
        "int16_t": 16,
        "int32_t": 32,
        "int64_t": 64,
    }
    UNSIGNED_SIZES = {
        "uint8_t": 8,
        "uint16_t": 16,
        "uint32_t": 32,
        "uint64_t": 64,
    }

    # TODO: This does checks on normal types, but if you pass a ctypes value
    #       in then this does not check those properly.

    if ptype == 'bool':
        return 'isinstance(%s, bool)' % pname

    elif ptype in ('float', 'double'):
        if strict:
            return 'isinstance(%s, (float))' % pname
        else:
            return 'isinstance(%s, (int, float))' % pname

    #elif ptype is C.c_char:
    #    return 'isinstance(%s, bytes) and len(%s) == 1' % (pname, pname)
    #elif ptype is C.c_wchar:
    #    return 'isinstance(%s, str) and len(%s) == 1' % (pname, pname)
    #elif ptype is C.c_char_p:
    #    return "%s is None or isinstance(%s, bytes) or getattr(%s, '_type_') is _C.c_char" % (pname, pname, pname)
    #elif ptype is C.c_wchar_p:
    #    return '%s is None or isinstance(%s, bytes)' % (pname, pname)

    elif ptype in ('int', 'long'):
        return 'isinstance(%s, int)' % pname
    elif ptype in SIGNED_SIZES:
        return SIGNED_CHECK(pname, SIGNED_SIZES[ptype] - 1)

    elif ptype == 'size_t':
        return 'isinstance(%s, int)' % (pname)
    elif ptype in UNSIGNED_SIZES:
        return UNSIGNED_CHECK(pname, UNSIGNED_SIZES[ptype])

    elif ptype is None:
        return '%s is None' % pname
    
    elif ptype == 'ctre::phoenix::ErrorCode':
        return 'isinstance(%s, ErrorCode)' % (pname,)

    else:
        # TODO: do validation here
        #return 'isinstance(%s, %s)' % (pname, type(ptype).__name__)
        return None

# fmt: on


def _to_annotation(ctypename):
    return _annotations[ctypename]


def header_hook(header, data):
    """Called for each header"""

    # fix enum names
    for e in header.enums:
        ename = e["name"].split("_")[0] + "_"
        for v in e["values"]:
            name = v["name"]
            if name.startswith(ename):
                name = name[len(ename) :]
            name = name.rstrip("_")
            if name == "None":
                name = "None_"
            elif name[0].isdigit():
                name = v["name"][0] + name
            v["x_name"] = name


def function_hook(fn, data):
    """Called for each function in the header"""

    # only output functions if a module name is defined
    if "module_name" not in data:
        return

    # Mangle the name appropriately
    m = re.match(r"c_%s_(.*)" % data["module_name"], fn["name"])
    if not m:
        raise Exception("Unexpected fn %s" % fn["name"])

    # Python exposed function name converted to camelcase
    x_name = m.group(1)
    x_name = x_name[0].lower() + x_name[1:]

    x_in_params = []
    x_out_params = []
    x_rets = []

    # Simulation assertions
    x_param_checks = []
    x_return_checks = []

    param_offset = 0 if x_name.startswith("create") else 1

    data = data.get("data", {}).get(fn["name"])
    if data is None:
        # ensure every function is in our yaml
        print("WARNING", fn["name"])
        data = {}
        # assert False, fn['name']

    param_defaults = data.get("defaults", {})
    param_override = data.get("param_override", {})

    for i, p in enumerate(fn["parameters"][param_offset:]):
        if p["name"] == "":
            p["name"] = "param%s" % i
        p["x_type"] = p["raw_type"]
        p["x_callname"] = p["name"]

        # Python annotations for sim
        p["x_pyann_type"] = _to_annotation(p["raw_type"])

        if p["name"] in param_override:
            p["pointer"] = 0
            p.update(param_override.pop(p["name"]))

        p["x_pyann"] = "%(name)s: %(x_pyann_type)s" % p
        p["x_pyarg"] = 'py::arg("%(name)s")' % p

        if p["name"] in param_defaults:
            _pname = param_defaults.pop(p["name"])
            p["x_pyann"] += " = " + str(_pname)
            p["x_pyarg"] += "=" + str(_pname)
        elif p["name"].lower() == "timeoutms":
            p["x_pyann"] += " = 0"
            p["x_pyarg"] += "=0"

        if p["pointer"]:
            p["x_callname"] = "&%(x_callname)s" % p
            x_out_params.append(p)
        elif p["array"]:
            asz = p.get("array_size", 0)
            if asz:
                p["x_pyann_type"] = "typing.List[%s]" % _to_annotation(p["raw_type"])
                p["x_type"] = "std::array<%s, %s>" % (p["x_type"], asz)
                p["x_callname"] = "%(x_callname)s.data()" % p
            else:
                # it's a vector
                pass

            x_out_params.append(p)
        else:
            chk = _gen_check(p["name"], p["x_type"])
            if chk:
                x_param_checks.append("assert %s" % chk)
            x_in_params.append(p)

        p["x_decl"] = "%s %s" % (p["x_type"], p["name"])

    assert not param_defaults
    assert not param_override

    x_callstart = ""
    x_callend = ""
    x_wrap_return = ""

    # Return all out parameters
    x_rets.extend(x_out_params)

    # if the function has out parameters and if the return value
    # is an error code, suppress the error code. This matches the Java
    # APIs, and the user can retrieve the error code from getLastError if
    # they really care
    if (not len(x_rets) or fn["rtnType"] != "ctre::phoenix::ErrorCode") and fn[
        "rtnType"
    ] not in ("void", "void *"):
        x_callstart = "auto __ret ="
        x_rets.insert(
            0,
            dict(
                name="__ret",
                x_type=fn["rtnType"],
                x_pyann_type=_to_annotation(fn["rtnType"]),
            ),
        )

        # Save some time in the common case -- set the error code to 0
        # if there's a single retval and the type is ErrorCode
        if fn["rtnType"] == "ctre::phoenix::ErrorCode":
            x_param_checks.append("retval = ErrorCode.OK")

    if len(x_rets) == 1 and x_rets[0]["x_type"] != "void":
        x_wrap_return = "return %s;" % x_rets[0]["name"]
        x_wrap_return_type = x_rets[0]["x_type"]
        x_pyann_ret = x_rets[0]["x_pyann_type"]
        chk = _gen_check("retval", x_wrap_return_type, strict=True)
        if chk:
            x_return_checks.append("assert %s" % chk)
    elif len(x_rets) > 1:
        x_pyann_ret = "typing.Tuple[%s]" % (
            ", ".join([p["x_pyann_type"] for p in x_rets]),
        )

        x_wrap_return = "return std::make_tuple(%s);" % ",".join(
            [p["name"] for p in x_rets]
        )
        x_wrap_return_type = "std::tuple<%s>" % (
            ", ".join([p["x_type"] for p in x_rets])
        )

        x_return_checks.append(
            "assert isinstance(retval, tuple) and len(retval) == %s" % len(x_rets)
        )
        for i, _p in enumerate(x_rets):
            chk = _gen_check("retval[%d]" % i, _p["raw_type"], strict=True)
            if chk:
                x_return_checks.append("assert %s" % chk)
    else:
        x_pyann_ret = "None"
        x_wrap_return_type = "void"

    # Temporary values to store out parameters in
    x_temprefs = ""
    if x_out_params:
        x_temprefs = ";".join(["%(x_type)s %(name)s" % p for p in x_out_params]) + ";"

    args_comma = ", " if x_in_params else ""

    if "return" in data.get("code", ""):
        raise ValueError("%s: Do not use return, assign to retval instead" % fn["name"])

    # Rename internal functions
    if data.get("internal", False):
        x_name = "_" + x_name
    if data.get("rename", False):
        x_name = data["rename"]

    name = fn["name"]

    hascode = "code" in data or "get" in data or "set" in data

    # lazy :)
    fn.update(locals())
