import functools
import logging
from collections import deque
from typing import Any, Optional

import ida_bytes
import ida_entry
import ida_funcs
import ida_idp
import ida_lines
import ida_name
import ida_segment
import ida_ua
import ida_xref
import ida_netnode
import wasm
import wasm.wasmtypes
from wasm.decode import Instruction, ModuleFragment

import idawasm.analysis.llvm
import idawasm.const
from idawasm.common import offset_of, size_of, struc_to_dict
from idawasm.types import Block, Data, Function, Global

logger = logging.getLogger(__name__)

# these are wasm-specific operand types
WASM_LOCAL = ida_ua.o_idpspec0
WASM_GLOBAL = ida_ua.o_idpspec1
WASM_FUNC_INDEX = ida_ua.o_idpspec2
WASM_TYPE_INDEX = ida_ua.o_idpspec3
WASM_BLOCK = ida_ua.o_idpspec4
WASM_ALIGN = ida_ua.o_idpspec5
WASM_BRANCH_TABLE = ida_ua.o_idpspec5+1
WASM_BRANCH_TABLE_DEFAULT = ida_ua.o_idpspec5+2


def no_exceptions(f):
    """
    decorator that catches and logs any exceptions.
    the exceptions are swallowed, and `0` is returned.

    this is useful for routines that IDA invokes, as IDA bails on exceptions.

    Example::

        @no_exceptions
        def definitely_doesnt_work():
            raise ZeroDivisionError()

        assert definitely_doesnt_work() == 0
    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        # we explicitly want to catch all exceptions here,
        # because IDA cannot handle them.
        except:  # NOQA: E722 do not use bare 'except'
            logger.error('exception in %s', f.__name__, exc_info=True)
            return 0

    return wrapper


# tags functions that are invoked from IDA-land.
ida_entry_point = no_exceptions


class SectionNotFoundError(Exception):
    def __init__(self, section_id):
        Exception.__init__(self, f'section not found: {section_id}')


class wasm_processor_t(ida_idp.processor_t):
    # processor ID for the wasm disassembler.
    # I made this number up.
    id = 0x8069
    flag = ida_idp.PR_USE32 | ida_idp.PR_RNAMESOK | ida_idp.PRN_HEX | ida_idp.PR_NO_SEGMOVE
    cnbits = 8
    dnbits = 8
    psnames = ['wasm']
    plnames = ['WebAssembly']
    segreg_size = 0
    tbyte_size = 0
    assembler = {
        'flag': ida_idp.ASH_HEXF3 | ida_idp.AS_UNEQU | ida_idp.AS_COLON | ida_idp.ASB_BINF4 | ida_idp.AS_N2CHR,
        'uflag': 0,
        'name': "WebAssembly assembler",
        'origin': "org",
        'end': "end",
        'cmnt': ";;",
        'ascsep': "\"",
        'accsep': "'",
        'esccodes': "\"'",
        'a_ascii': "db",
        'a_byte': "db",
        'a_word': "dw",
        'a_dword': "dd",
        'a_qword': "dq",
        'a_oword': "xmmword",
        'a_float': "dd",
        'a_double': "dq",
        'a_tbyte': "dt",
        'a_dups': "#d dup(#v)",
        'a_bss': "%s dup ?",
        'a_seg': "seg",
        'a_curip': "$",
        'a_public': "public",
        'a_weak': "weak",
        'a_extrn': "extrn",
        'a_comdef': "",
        'a_align': "align",
        'lbrace': "(",
        'rbrace': ")",
        'a_mod': "%",
        'a_band': "&",
        'a_bor': "|",
        'a_xor': "^",
        'a_bnot': "~",
        'a_shl': "<<",
        'a_shr': ">>",
        'a_sizeof_fmt': "size %s",
    }

    def dt_to_width(self, dt):
        """
        returns OOFW_xxx flag given a dt_xxx
        """
        return {
            ida_ua.dt_byte: ida_ua.OOFW_8,
            ida_ua.dt_word: ida_ua.OOFW_16,
            ida_ua.dt_dword: ida_ua.OOFW_32,
            ida_ua.dt_qword: ida_ua.OOFW_64,
            ida_ua.dt_float: ida_ua.OOFW_32,
            ida_ua.dt_double: ida_ua.OOFW_64,
        }[dt]

    def _get_section(self, section_id: int) -> ModuleFragment:
        """
        fetch the section with the given id.

        Args:
          section_id (int): the section id.

        Returns:
          wasm.Structure: the section.

        Raises:
          SectionNotFoundError: if the section is not found.
        """
        for i, section in enumerate(self.sections):
            if i == 0:
                continue

            if section.data.id != section_id:
                continue

            return section

        raise SectionNotFoundError(section_id)

    def _get_section_offset(self, section_id: int) -> int:
        """
        fetch the file offset of the given section.

        Args:
          section_id (int): the section id.

        Returns:
          int: the offset of the section.

        Raises:
          SectionNotFoundError: if the section is not found.
        """
        p = 0
        for i, section in enumerate(self.sections):
            if i == 0:
                p += size_of(section.data)
                continue

            if section.data.id != section_id:
                p += size_of(section.data)
                continue

            return p

        raise SectionNotFoundError(section_id)

    def _compute_function_branch_targets(self, offset: int, code: bytes) -> dict[int, dict[str, Block]]:
        """
        compute branch targets for the given code segment.

        we can do it in a single pass:
        scan instructions, tracking new blocks, and maintaining a stack of nested blocks.
        when we hit a branch instruction, use the stack to resolve the branch target.
        the branch target will always come from the enclosing scope.

        Args:
          offset (int): offset of the given code segment.
          code (bytes): raw bytecode.

        Returns:
          dict[int, dict[str, Block]]: map from instruction addresses to map from relative depth to branch target address.
        """
        # map from virtual address to map from relative depth to virtual address
        branch_targets: dict[int, dict[str, Block]] = {}
        # map from block index to block instance, with fields including `offset` and `depth`
        blocks: dict[int, Block] = {}
        # stack of block indexes
        block_stack: deque[int] = deque()
        p = offset

        for bc in wasm.decode_bytecode(code):
            if bc.op.id in {wasm.OP_BLOCK, wasm.OP_LOOP, wasm.OP_IF}:
                # enter a new block, so capture info, and push it onto the current depth stack
                block_index = len(blocks)
                block: Block = {
                    'index': block_index,
                    'offset': p,
                    'end_offset': None,
                    'else_offset': None,
                    'br_table_target': None,
                    'depth': len(block_stack),
                    'type': {
                        wasm.OP_BLOCK: 'block',
                        wasm.OP_LOOP: 'loop',
                        wasm.OP_IF: 'if',
                    }[bc.op.id],
                }
                blocks[block_index] = block
                block_stack.appendleft(block_index)
                branch_targets[p] = {
                    # reference to block that is starting
                    'block': block
                }

            elif bc.op.id in {wasm.OP_END}:
                if len(block_stack) == 0:
                    # end of function
                    branch_targets[p] = {
                        'block': {
                            'type': 'function',
                            'offset': offset,  # start of function
                            'end_offset': p,  # end of function
                            'depth': 0,  # top level always has depth 0
                        }
                    }
                    break

                # leaving a block, so pop from the depth stack
                block_index = block_stack.popleft()
                block = blocks[block_index]
                block['end_offset'] = p + bc.len
                branch_targets[p] = {
                    # reference to block that is ending
                    'block': block
                }

                br_table_target = block['br_table_target']
                if br_table_target is not None:
                    ida_bytes.set_cmt(block['end_offset'], 'table %d' % br_table_target, 0)

            elif bc.op.id in {wasm.OP_BR, wasm.OP_BR_IF}:
                block_index = block_stack[bc.imm.relative_depth]
                block = blocks[block_index]
                branch_targets[p] = {
                    bc.imm.relative_depth: block
                }

            elif bc.op.id in {wasm.OP_ELSE}:
                for block_index in block_stack:
                    block = blocks[block_index]
                    if block['type'] == 'if':
                        block['else_offset'] = p + bc.len
                        branch_targets[p] = {
                            # reference to block that is ending
                            'block': block,
                        }
                        break

            elif bc.op.id in {wasm.OP_BR_TABLE}:
                branch_targets[p] = {}
                for relative_depth in *bc.imm.target_table, bc.imm.default_target:
                    block_index = block_stack[relative_depth]
                    block = blocks[block_index]
                    block['br_table_target'] = relative_depth
                    branch_targets[p][relative_depth] = block

            p += bc.len

        return branch_targets

    def _compute_branch_targets(self) -> dict[int, dict[str, Block]]:
        branch_targets: dict[int, dict[str, Block]] = {}

        code_section = self._get_section(wasm.wasmtypes.SEC_CODE)
        pcode_section = self._get_section_offset(wasm.wasmtypes.SEC_CODE)

        ppayload = pcode_section + offset_of(code_section.data, 'payload')
        pbody = ppayload + offset_of(code_section.data.payload, 'bodies')
        for body in code_section.data.payload.bodies:
            pcode = pbody + offset_of(body, 'code')
            branch_targets.update(self._compute_function_branch_targets(pcode, body.code))
            pbody += size_of(body)

        return branch_targets

    def _parse_types(self) -> list[dict[str, Any]]:
        """
        parse the type entries.

        Returns:
          list[dict[str, Any]]: list if type descriptors, each which hash:
            - form
            - param_count
            - param_types
            - return_count
            - return_type
        """
        type_section = self._get_section(wasm.wasmtypes.SEC_TYPE)
        return struc_to_dict(type_section.data.payload.entries)

    def _parse_imported_globals(self) -> dict[int, Global]:
        """
        parse the import entries for globals.
        """
        globals_: dict[int, Global] = {}
        import_section = self._get_section(wasm.wasmtypes.SEC_IMPORT)
        pimport_section = self._get_section_offset(wasm.wasmtypes.SEC_IMPORT)

        ppayload = pimport_section + offset_of(import_section.data, 'payload')
        pentries = ppayload + offset_of(import_section.data.payload, 'entries')
        pcur = pentries
        i = 0
        for body in import_section.data.payload.entries:
            if body.kind == idawasm.const.WASM_EXTERNAL_KIND_GLOBAL:
                ctype = idawasm.const.WASM_TYPE_NAMES[body.type.content_type]
                module = body.module_str.tobytes().decode('utf-8')
                field = body.field_str.tobytes().decode('utf-8')
                globals_[i] = {
                    'index': i,
                    'offset': pcur,
                    'type': ctype,
                    'name': f'{module}.{field}',
                }

                i += 1
            pcur += size_of(body)

        return globals_

    def _parse_globals(self) -> dict[int, Global]:
        """
        parse the global entries.

        Returns:
          dict[int, Global]: from global index to dict with keys `offset` and `type`.
        """
        globals_: dict[int, Global] = {}

        globals_.update(self._parse_imported_globals())

        global_section = self._get_section(wasm.wasmtypes.SEC_GLOBAL)
        pglobal_section = self._get_section_offset(wasm.wasmtypes.SEC_GLOBAL)

        ppayload = pglobal_section + offset_of(global_section.data, 'payload')
        pglobals = ppayload + offset_of(global_section.data.payload, 'globals')
        pcur = pglobals
        i = len(globals_)
        for body in global_section.data.payload.globals:
            pinit = pcur + offset_of(body, 'init')
            ctype = idawasm.const.WASM_TYPE_NAMES[body.type.content_type]

            name = 'global_%X' % i

            # get name from imported global in a case like:
            # 02FA sections:6:payload:globals:3:init
            # 02FA
            # 02FA _env_STACKTOP:
            # 02FA                 get_global          env_STACKTOP
            # 02FC                 end
            if len(body.init) > 0:
                bc = body.init[0]
                if bc.op.id == wasm.OP_GET_GLOBAL:
                    global_index = bc.imm.global_index
                    if global_index in globals_:
                        name = '_' + globals_[global_index]['name']

            globals_[i] = {
                'index': i,
                'offset': pinit,
                'type': ctype,
                'name': name,
            }

            i += 1
            pcur += size_of(body)

        for global_ in globals_.values():
            ida_name.set_name(global_['offset'], global_['name'], ida_name.SN_CHECK)

        return globals_

    def _parse_imported_functions(self) -> dict[int, dict[str, Any]]:
        """
        parse the import entries for functions.
        useful for recovering function names.

        Returns:
          dict[int, dict[str, any]]: from function index to dict with keys `index`, `module`, and `name`.
        """
        functions: dict[int, Function] = {}
        import_section = self._get_section(wasm.wasmtypes.SEC_IMPORT)
        type_section = self._get_section(wasm.wasmtypes.SEC_TYPE)

        function_index = 0
        for entry in import_section.data.payload.entries:
            if entry.kind != idawasm.const.WASM_EXTERNAL_KIND_FUNCTION:
                continue

            type_index = entry.type.type
            ftype = type_section.data.payload.entries[type_index]

            functions[function_index] = {
                'index': function_index,
                'module': entry.module_str.tobytes().decode('utf-8'),
                'name': entry.field_str.tobytes().decode('utf-8'),
                'type': struc_to_dict(ftype),
                'imported': True,
                # TODO: not sure if an import can be exported.
                'exported': False,
            }

            function_index += 1

        return functions

    def _parse_exported_functions(self) -> dict[int, dict[str, Any]]:
        """
        parse the export entries for functions.
        useful for recovering function names.

        Returns:
          dict[int, dict[str, any]]: from function index to dict with keys `index` and `name`.
        """
        functions: dict[int, Function] = {}
        export_section = self._get_section(wasm.wasmtypes.SEC_EXPORT)
        for entry in export_section.data.payload.entries:
            if entry.kind != idawasm.const.WASM_EXTERNAL_KIND_FUNCTION:
                continue

            functions[entry.index] = {
                'index': entry.index,
                'name': entry.field_str.tobytes().decode('utf-8'),
                'exported': True,
                # TODO: not sure if an export can be imported.
                'imported': False,
            }

        return functions

    def _parse_functions(self) -> dict[int, Function]:
        try:
            imported_functions = self._parse_imported_functions()
        except SectionNotFoundError:
            imported_functions = {}
        try:
            exported_functions = self._parse_exported_functions()
        except SectionNotFoundError:
            exported_functions = []

        functions: dict[int, Function] = dict(imported_functions)

        function_section = self._get_section(wasm.wasmtypes.SEC_FUNCTION)
        code_section = self._get_section(wasm.wasmtypes.SEC_CODE)
        pcode_section = self._get_section_offset(wasm.wasmtypes.SEC_CODE)
        type_section = self._get_section(wasm.wasmtypes.SEC_TYPE)

        payload = code_section.data.payload
        ppayload = pcode_section + offset_of(code_section.data, 'payload')
        pbody = ppayload + offset_of(payload, 'bodies')
        for i in range(code_section.data.payload.count):
            function_index = len(imported_functions) + i
            body = code_section.data.payload.bodies[i]
            type_index = function_section.data.payload.types[i]
            ftype = type_section.data.payload.entries[type_index]

            local_types = []
            for locals_group in body.locals:
                ltype = locals_group.type
                for j in range(locals_group.count):
                    local_types.append(ltype)

            if function_index in exported_functions:
                name = exported_functions[function_index]['name']
                is_exported = True
            else:
                name = '$func%d' % function_index
                is_exported = False

            functions[function_index] = {
                'index': function_index,
                'name': name,
                'offset': pbody + offset_of(body, 'code'),
                'type': struc_to_dict(ftype),
                'exported': is_exported,
                'imported': False,
                'local_types': local_types,
                'size': size_of(body, 'code'),
            }

            pbody += size_of(body)

        return functions

    def _parse_data(self) -> dict[int, Data]:
        data: dict[int, Data] = {}
        data_section = self._get_section(wasm.wasmtypes.SEC_DATA)
        pdata_section = self._get_section_offset(wasm.wasmtypes.SEC_DATA)

        ppayload = pdata_section + offset_of(data_section.data, 'payload')
        pentries = ppayload + offset_of(data_section.data.payload, 'entries')
        pcur = pentries
        i = 0
        for entry in data_section.data.payload.entries:
            ea = pcur + size_of(entry, 'index') \
                 + size_of(entry, 'offset') \
                 + size_of(entry, 'size')
            offset = 0
            if len(entry.offset) > 0:
                bc = entry.offset[0]
                if bc.op.id == wasm.OP_I32_CONST:
                    offset = bc.imm.value

            data[i] = {
                'index': i,
                'offset': offset,
                'ea': ea,
                'size': entry.size,
                'data': entry.data.tobytes(),
            }

            i += 1
            pcur += size_of(entry)

        return data

    def _render_type(self, type_, name=None):
        if name is None:
            name = ''
        else:
            name = ' ' + name

        params = []
        if type_['param_count'] > 0:
            for i, param in enumerate(type_['param_types']):
                params.append(' (param $param%d %s)' % (i, idawasm.const.WASM_TYPE_NAMES[param]))
        sparam = ''.join(params)

        if type_['return_count'] == 0:
            sresult = ''
        elif type_['return_count'] == 1:
            sresult = ' (result %s)' % (idawasm.const.WASM_TYPE_NAMES[type_['return_type']])
        else:
            raise NotImplementedError('multiple return values')

        return '(func%s%s%s)' % (name, sparam, sresult)

    def _render_function_prototype(self, function) -> str:
        if function.get('imported'):
            name = '$import%d' % (function['index'])
            signature = self._render_type(function['type'], name=name)
            return '(import "%s" "%s" %s)' % (function['module'],
                                              function['name'],
                                              signature)
        else:
            return self._render_type(function['type'], name=function['name'])

    def _render_branch_table(self, addr) -> str:
        bc = self._decode_bytecode_at(addr)
        return '[%s]' % ','.join(map(str, bc.imm.target_table))

    def load(self):
        """
        load the state of the processor and analysis from the segments.

        the processor object may not be re-created, so we do our initialization here.
        initialize the following fields:

          - self.buf
          - self.sections
          - self.functions
          - self.function_offsets
          - self.function_ranges
          - self.globals
          - self.branch_targets
        """
        logger.info('parsing sections')
        buf = []
        for n in range(ida_segment.get_segm_qty()):
            # assume all the segments are contiguous, which is what our loader does
            seg = ida_segment.getnseg(n)
            if seg:
                buf.append(ida_bytes.get_bytes(seg.start_ea, seg.end_ea - seg.start_ea))

        self.buf = b''.join(buf)
        self.sections = list(wasm.decode_module(self.buf))

        logger.info('parsing types')
        try:
            self.types = self._parse_types()
        except SectionNotFoundError as e:
            logger.info(f'failed to parse types: {e}')

        logger.info('parsing globals')
        try:
            self.globals = self._parse_globals()
        except SectionNotFoundError as e:
            logger.info(f'failed to parse globals: {e}')

        logger.info('parsing functions')
        try:
            self.functions = self._parse_functions()
        except SectionNotFoundError as e:
            logger.info(f'failed to parse functions: {e}')

        # map from function offset to function object
        self.function_offsets = {f['offset']: f for f in self.functions.values() if 'offset' in f}

        # map from (function start, function end) to function object
        self.function_ranges = {
            (f['offset'], f['offset'] + f['size']): f
            for f in self.functions.values()
            if 'offset' in f
        }

        logger.info('parsing data')
        try:
            self.data = self._parse_data()
        except SectionNotFoundError as e:
            logger.info(f'failed to parse data: {e}')

        logger.info('computing branch targets')
        self.branch_targets = self._compute_branch_targets()

        self.deferred_noflows = {}
        self.deferred_flows = {}

        for function in self.functions.values():
            name = function['name']
            if 'offset' in function:
                ida_name.set_name(function['offset'], name, ida_name.SN_CHECK)
                # notify_emu will be invoked from here.
                ida_ua.create_insn(function['offset'])
                ida_funcs.add_func(function['offset'], function['offset'] + function['size'])

            if function.get('exported'):
                # TODO: this should really be done in the loader.
                # though, at the moment, we do a lot more analysis here in the processor.
                ida_entry.add_entry(function['index'], function['offset'], name, True)

            # TODO: ida_entry.add_entry for the start routine. need an example of this.

    @ida_entry_point
    def ev_newfile(self, filename: str) -> int:
        """
        handle file being analyzed for the first time.
        """
        logger.info('new file: %s', filename)
        self.load()

        wasm_nn = ida_netnode.Netnode('$ wasm.offsets')
        wasm_nn['functions'] = {f['index']: f['offset'] for f in self.functions.values() if 'offset' in f}
        wasm_nn['globals'] = {g['index']: g['offset'] for g in self.globals.values() if 'offset' in g}

        for Analyzer in (idawasm.analysis.llvm.LLVMAnalyzer,):
            ana = Analyzer(self)

            if ana.taste():
                logger.debug('%s analyzing', Analyzer.__name__)
                ana.analyze()
            else:
                logger.debug('%s declined analysis', Analyzer.__name__)

        return 0

    @ida_entry_point
    def ev_oldfile(self, filename: str) -> int:
        """
        handle file loaded from existing .idb database.
        """
        logger.info('existing database: %s', filename)
        self.load()

        return 0

    @ida_entry_point
    def savebase(self) -> None:
        """
        the database is being saved.
        """
        logger.info('saving wasm processor state.')

    @ida_entry_point
    def ev_endbinary(self, ok: bool) -> None:
        """
         After loading a binary file
         args:
          ok - file loaded successfully?
        """
        logger.info('wasm module loaded.')

    @ida_entry_point
    def ev_get_autocmt(self, insn: ida_ua.insn_t) -> Optional[str]:
        """
        fetch instruction auto-comment.

        Returns:
          Optional[str]: the comment string, or None.
        """
        if 'cmt' in self.instruc[insn.itype]:
            return self.instruc[insn.itype]['cmt']

    @ida_entry_point
    def ev_may_be_func(self, insn: ida_ua.insn_t, state) -> int:
        """
        can a function start at the given instruction?

        Returns:
          int: 100 if a function starts here, zero otherwise.
        """
        if insn.ea in self.function_offsets:
            return 100
        else:
            return 0

    def notify_emu_BR_END(self, insn: ida_ua.insn_t, next: ida_ua.insn_t) -> int:
        # unconditional branch followed by END.

        # BR flows to the END
        ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

        # unconditional branch, so END does not flow to following instruction
        self.deferred_noflows[next.ea] = True

        # branch target
        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            target_block = targets[insn.Op1.value]
            target_va = target_block['end_offset']
            self.deferred_flows[next.ea] = [(next.ea, target_va, ida_xref.fl_JF)]

        return 1

    def notify_emu_BR_IF_END(self, insn: ida_ua.insn_t, next: ida_ua.insn_t) -> int:
        # BR_IF flows to the END
        ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

        # conditional branch, so there will be a fallthrough flow.
        # the default behavior of `end` is to fallthrough, so don't change that.
        pass

        # branch target
        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            target_block = targets[insn.Op1.value]
            target_va = target_block['end_offset']
            self.deferred_flows[next.ea] = [(next.ea, target_va, ida_xref.fl_JF)]

        return 1

    def notify_emu_BR_TABLE_END(self, insn: ida_ua.insn_t, next: ida_ua.insn_t) -> int:
        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            for target_block in targets.values():
                target_va = target_block['end_offset']
                ida_xref.add_cref(insn.ea, target_va, ida_xref.fl_JF)

        return 1

    def notify_emu_RETURN_END(self, insn: ida_ua.insn_t, next: ida_ua.insn_t) -> int:
        # the RETURN will fallthrough to END,
        ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

        # but the END will not fallthrough.
        self.deferred_noflows[next.ea] = True

        return 1

    def notify_emu_UNREACHABLE_END(self, insn: ida_ua.insn_t, next: ida_ua.insn_t) -> int:
        # but the END will not fallthrough.
        self.deferred_noflows[next.ea] = True

        return 0

    def notify_emu_BR(self, insn: ida_ua.insn_t) -> int:
        # handle an unconditional branch not at the end of a black.

        # unconditional branch does not fallthrough flow.
        pass

        # branch target
        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            target_block = targets[insn.Op1.value]
            target_va = target_block['end_offset']
            ida_xref.add_cref(insn.ea, target_va, ida_xref.fl_JF)

        return 1

    def notify_emu_BR_IF(self, insn: ida_ua.insn_t) -> int:
        # handle a conditional branch not at the end of a block.
        # fallthrough flow
        ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

        # branch target
        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            target_block = targets[insn.Op1.value]
            target_va = target_block['end_offset']
            ida_xref.add_cref(insn.ea, target_va, ida_xref.fl_JF)

        return 1

    def notify_emu_IF(self, insn: ida_ua.insn_t) -> int:
        ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            for target_block in targets.values():
                else_va = target_block['else_offset']
                if else_va:
                    ida_xref.add_cref(insn.ea, else_va, ida_xref.fl_JF)
                else:
                    end_va = target_block['end_offset']
                    ida_xref.add_cref(insn.ea, end_va, ida_xref.fl_JF)

        return 1

    def notify_emu_ELSE(self, insn: ida_ua.insn_t) -> int:
        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            for target_block in targets.values():
                target_va = target_block['end_offset']
                ida_xref.add_cref(insn.ea, target_va, ida_xref.fl_JF)

        return 1

    def notify_emu_END(self, insn: ida_ua.insn_t) -> int:
        for flow in self.deferred_flows.get(insn.ea, []):
            ida_xref.add_cref(*flow)

        if insn.ea in self.branch_targets:
            targets = self.branch_targets[insn.ea]
            block = targets['block']
            if block['type'] == 'loop':
                # end of loop

                # noflow

                # branch back to top of loop
                target_va = block['offset']
                ida_xref.add_cref(insn.ea, target_va, ida_xref.fl_JF)

            elif block['type'] == 'if':
                # end of if
                if insn.ea not in self.deferred_noflows:
                    ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

            elif block['type'] == 'block':
                # end of block
                # fallthrough flow, unless a deferred noflow from earlier, such as the case:
                #
                #     return
                #     end
                #
                # the RETURN is the end of the function, so no flow after the END.
                if insn.ea not in self.deferred_noflows:
                    ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

            elif block['type'] == 'function':
                # end of function
                # noflow
                pass

            else:
                raise RuntimeError('unexpected block type: ' + block['type'])

        return 1

    @ida_entry_point
    def ev_emu_insn(self, insn: ida_ua.insn_t) -> int:
        """
        Emulate instruction, create cross-references, plan to analyze
        subsequent instructions, modify flags etc. Upon entrance to this function
        all information about the instruction is in 'insn' structure.
        If zero is returned, the kernel will delete the instruction.

        adding xrefs is fairly straightforward, except for one hiccup:
        we'd like xrefs to flow from trailing END instructions,
         rather than getting orphaned in their own basic block.

        for example, consider the following:

            br $block0
            end

        if we place the code flow xref on the BR,
         then there is no flow to the END instruction,
         and the graph will look like:

            +------------+     +-----+
            |     ...    |     | end |
            | br $block0 |     +-----+
            +------------+
                   |
                  ...

        instead, we want the code flow xref to flow from the END,
         deferred from the BR, so the graph looks like this:

            +------------+
            |     ...    |
            | br $block0 |
            | end        |
            +------------+
                   |
                  ...

        to do this, at branching instruction,
         we detect if the following instruction is an END.
        if so, we flow through to the END,
         and queue the xrefs to be added when the END is processed.

        this assumes that the branching instructions are always analyzed before the END instructions.

        unfortunately, adding xrefs on subsequent instructions doesn't work (the node doesn't exist, or something).
        so, we have to used this "deferred" approach.
        """

        next = ida_ua.insn_t()
        if not ida_ua.decode_insn(next, insn.ea + insn.size):
            next = None

        # add drefs to globals
        for op in insn.ops:
            if not (op.type == ida_ua.o_imm and op.specval == WASM_GLOBAL):
                continue

            if op.value not in self.globals:
                logger.debug('missing global: %d', op.value)
                continue

            global_va = self.globals[op.value]['offset']
            if insn.itype == self.itype_SET_GLOBAL:
                ida_xref.add_dref(insn.ea, global_va, ida_xref.dr_W)
            elif insn.itype == self.itype_GET_GLOBAL:
                ida_xref.add_dref(insn.ea, global_va, ida_xref.dr_R)
            else:
                raise RuntimeError('unexpected instruction referencing global: ' + str(insn))

        # add drefs to data
        for op in insn.ops:
            if op.type == ida_ua.o_imm and op.dtype == ida_ua.dt_dword:
                va = op.value
                for data in self.data.values():
                    if data['offset'] <= va <= data['offset'] + data['size']:
                        ida_xref.add_dref(insn.ea, va - data['offset'] + data['ea'], ida_xref.dr_R)

        # TODO: add drefs to memory, but need example of this first.

        # handle cases like:
        #
        #     block
        #     ...
        #     br $foo
        #     end
        #
        # we want the cref to flow from the instruction `end`, not `br $foo`.
        if (insn.itype in {self.itype_BR,
                           self.itype_BR_IF,
                           self.itype_BR_TABLE}
                and next is not None  # NOQA: E127 continuation line over-indented for visual indent
                and next.itype == self.itype_END):  # NOQA: E127

            if insn.itype == self.itype_BR:
                return self.notify_emu_BR_END(insn, next)

            elif insn.itype == self.itype_BR_IF:
                return self.notify_emu_BR_IF_END(insn, next)

            elif insn.itype == self.itype_BR_TABLE:
                return self.notify_emu_BR_TABLE_END(insn, next)

        # handle cases like:
        #
        #     ...
        #     return
        #     end
        #
        # we want return to flow into the return, which should then not flow.
        elif (insn.itype == self.itype_RETURN
              and next is not None
              and next.itype == self.itype_END):
            return self.notify_emu_RETURN_END(insn, next)

        # handle cases like:
        #
        #     ...
        #     unreachable
        #     end
        elif (insn.itype == self.itype_UNREACHABLE
              and next is not None
              and next.itype == self.itype_END):
            return self.notify_emu_UNREACHABLE_END(insn, next)

        # handle cases like:
        #
        #     ...
        #     br $foo
        #     unreachable
        elif (insn.itype == self.itype_BR
              and next is not None
              and next.itype == self.itype_UNREACHABLE):
            return 1

        # handle other RETURN and UNREACHABLE instructions.
        # tbh, not sure how we'd encounter another RETURN, but we'll be safe.
        elif insn.get_canon_feature() & wasm.INSN_NO_FLOW:
            return 1

        # handle an unconditional branch not at the end of a black.
        elif insn.itype == self.itype_BR:
            return self.notify_emu_BR(insn)

        elif insn.itype == self.itype_BR_TABLE:
            # haven't seen one of these yet, so don't know to handle exactly.
            raise NotImplementedError('br table')

        # handle a conditional branch not at the end of a block.
        elif insn.itype == self.itype_BR_IF:
            return self.notify_emu_BR_IF(insn)

        elif insn.itype == self.itype_IF:
            return self.notify_emu_IF(insn)

        elif insn.itype == self.itype_ELSE:
            return self.notify_emu_ELSE(insn)

        # add flows deferred from a prior branch, eg.
        #
        #     br $foo
        #     end
        #
        # flows deferred from the BR to the END insn.
        elif insn.itype == self.itype_END:
            return self.notify_emu_END(insn)

        # default behavior: fallthrough
        else:
            ida_xref.add_cref(insn.ea, insn.ea + insn.size, ida_xref.fl_F)

    @ida_entry_point
    def out_mnem(self, ctx) -> None:
        postfix = ''
        ctx.out_mnem(20, postfix)

    def _get_function(self, ea):
        """
        fetch the function object that contains the given address.
        """
        # warning: O(#funcs) scan here, called in a tight loop (render operand).
        for (start, end), f in self.function_ranges.items():
            if start <= ea < end:
                return f
        raise KeyError(ea)

    @ida_entry_point
    def ev_out_operand(self, ctx, op):
        """
        Generate text representation of an instruction operand.
        This function shouldn't change the database, flags or anything else.
        All these actions should be performed only by u_emu() function.
        The output text is placed in the output buffer initialized with init_output_buffer()
        This function uses out_...() functions from ua.hpp to generate the operand text
        Returns: 1-ok, 0-operand is hidden.
        """
        if op.type == WASM_BLOCK:
            if op.value == 0xFFFFFFFFFFFFFFC0:  # VarInt7 for 0x40
                # block has empty type
                pass
            else:
                # ref: https://webassembly.github.io/spec/core/binary/types.html#binary-valtype
                # TODO(wb): untested!
                ctx.out_keyword({
                                    # TODO(wb): I don't think these constants will line up in practice
                                    0x7F: 'type:i32',
                                    0x7E: 'type:i64',
                                    0x7D: 'type:f32',
                                    0x7C: 'type:f64',
                                }[op.value])
            return True

        elif op.type == ida_ua.o_reg:
            wtype = op.specval
            if wtype == WASM_LOCAL:
                # output a function-local "register".
                # these are nice because they can be re-named by the analyst.
                #
                # eg.
                #     code:0D57    get_local    $param0
                #     code:0D4B    set_local    $local9
                #                                 ^
                #                                these things
                f = self._get_function(ctx.insn.ea)
                if op.reg < f['type']['param_count']:
                    # the first `param_count` indices reference a parameter,
                    ctx.out_register('$param%d' % op.reg)
                else:
                    # and the remaining indices are local variables.
                    ctx.out_register('$local%d' % op.reg)
                return True

        elif op.type == ida_ua.o_imm:
            wtype = op.specval
            if wtype == WASM_GLOBAL:
                # output a reference to a global variable.
                # note that we provide the address of the variable,
                #  and IDA will insert the correct name.
                # this is particularly nice when a user re-names the variable.
                #
                # eg.
                #
                #     code:0D38    set_global   global_0
                #                                 ^
                #                                this thing
                if op.value in self.globals:
                    g = self.globals[op.value]
                    ctx.out_name_expr(op, g['offset'])
                    return True
                else:
                    logger.info('missing global at index %d', op.value)
                    ctx.out_register('$global%d' % op.value)
                    return True

            elif wtype == WASM_FUNC_INDEX:
                f = self.functions[op.value]
                if 'offset' in f:
                    # output a reference to an existing function.
                    # note that we provide the address of the function,
                    #  and IDA will insert the correct name.
                    #
                    # eg.
                    #
                    #     code:0D9E    call   $func9
                    #                           ^
                    #                          this thing
                    ctx.out_name_expr(op, f['offset'])
                else:
                    # output a reference to a function by name,
                    # such as an imported routine.
                    # since this won't have a location in the binary,
                    #  we output the raw name of the function.
                    #
                    # TODO: link this to the import entry
                    ctx.out_keyword(f['name'])
                return True

            elif wtype == WASM_TYPE_INDEX:
                # resolve the type index into a type,
                # then human-render it.
                #
                # eg.
                #
                #     code:0B7F  call_indirect  (func (param $param0 i32) (param $param1 i32) (result i32)), 0
                #                  ^
                #                 this thing
                type_index = op.value
                type = self.types[type_index]
                signature = self._render_type(type)

                ctx.out_keyword(signature)
                return True

            elif wtype == WASM_ALIGN:
                # output an alignment directive.
                #
                # eg.
                #
                #     code:0B54   i32.load    0x30, align:2
                #                                     ^
                #                                    this thing
                ctx.out_keyword('align:')
                width = self.dt_to_width(op.dtype)
                ctx.out_value(op, ida_ua.OOFW_IMM | width)
                return True

            elif wtype == WASM_BRANCH_TABLE:
                # output a branch table.
                #
                # eg.
                #
                #     code:XXXX   br_table    3, [0,1,2], default:0
                #                                  ^
                #                                 this thing
                branch_table = self._render_branch_table(op.addr)
                ctx.out_keyword(branch_table)
                return True

            elif wtype == WASM_BRANCH_TABLE_DEFAULT:
                # output a default branch target.
                #
                # eg.
                #
                #     code:XXXX   br_table    3, [0,1,2], default:0
                #                                           ^
                #                                          this thing
                ctx.out_keyword('default:')
                ctx.out_long(op.value, 10)
                return True

            else:
                width = self.dt_to_width(op.dtype)
                ctx.out_value(op, ida_ua.OOFW_IMM | width)
                return True

        # error case
        return False

    @ida_entry_point
    def ev_out_insn(self, ctx):
        """
        must not change the database.

        args:
          ctx (object): has a `.insn` field.
        """
        insn = ctx.insn
        ea = insn.ea

        # if this is the start of a function, render the function prototype.
        # like::
        #
        #     code:082E $func8:
        #     code:082E (func $func8 (param $param0 i32) (param $param1 i32) (result i32))
        if ea in self.function_offsets:
            # use idaapi.rename_regvar and idaapi.find_regvar to resolve $local/$param names
            # ref: https://reverseengineering.stackexchange.com/q/3038/17194
            fn = self.function_offsets[ea]
            proto = self._render_function_prototype(fn)
            ctx.gen_printf(0, proto + '\n')

        # the instruction has a mnemonic, then zero or more operands.
        # if more than one operand, the operands are separated by commas.
        #
        # eg.
        #
        #     code:0E30    i32.store    0x1C,  align:2
        #                      ^         ^  ^ ^     ^
        #                  mnemonic      |  | |     |
        #                             op[0] | |     |
        #                               comma |     |
        #                                     space |
        #                                        op[1]

        ctx.out_mnemonic()
        ctx.out_one_operand(0)

        for i in range(1, 3):
            op = insn[i]

            if op.type == ida_ua.o_void:
                break

            ctx.out_symbol(',')
            ctx.out_char(' ')
            ctx.out_one_operand(i)

        # if this is a block instruction, annotate the relevant block.
        #
        # eg.
        #
        #     code:0E84     block        $block2
        #     code:0E86     loop         $loop3
        #     code:0F3F     end          $loop3
        #                                   ^
        #                                 this name

        # TODO: resolve block names on conditionals.
        # right now they look like:
        #
        #     code:0E77     br_if        1
        #
        # but we want something like this:
        #
        #     code:0E77     br_if        $block2

        # TODO: even better, we should use the location name, rather than auto-generated $block name
        # from this:
        #
        #     code:0E77     br_if        $block2
        #
        # want:
        #
        #     code:0E77     br_if        loc_error

        if insn.itype in (self.itype_BLOCK, self.itype_LOOP, self.itype_IF, self.itype_END) \
                and ea in self.branch_targets:

            targets = self.branch_targets[ea]
            block = targets['block']
            if block['type'] in ('block', 'loop', 'if'):
                ctx.out_tagon(ida_lines.COLOR_UNAME)
                for c in ("$" + block['type'] + str(block['index'])):
                    ctx.out_char(c)
                ctx.out_tagoff(ida_lines.COLOR_UNAME)

        ctx.set_gen_cmt()
        ctx.flush_outbuf()

    def _decode_bytecode_at(self, addr: int) -> Instruction:
        for i in range(1, 5):
            try:
                buf = ida_bytes.get_bytes(addr, 0x10 ** i)
                bc = next(wasm.decode_bytecode(buf))
                return bc
            except:  # NOQA: E722 do not use bare 'except'
                pass

        raise RuntimeError('could not decode bytecode')

    @ida_entry_point
    def ev_ana_insn(self, insn: ida_ua.insn_t) -> int:
        """
        decodes an instruction and place it into the given insn.

        Args:
          insn (ida_ua.insn_t): the instruction to populate.

        Returns:
          int: size of insn on success, 0 on failure.
        """

        # as of today (v1), each opcode is a single byte
        opb = insn.get_next_byte()

        if opb not in wasm.opcodes.OPCODE_MAP:
            return 0

        # translate from opcode index to IDA-specific const.
        # as you can see elsewhere, IDA insn consts have to be contiguous,
        #  so we can't just re-use the opcode index.
        insn.itype = self.insns[opb]['id']

        # fetch entire instruction buffer to decode
        if wasm.opcodes.OPCODE_MAP.get(opb).imm_struct:
            # opcode has operands that we must decode

            bc = self._decode_bytecode_at(insn.ea)
        else:
            # single byte instruction

            buf = bytes([opb])
            bc = next(wasm.decode_bytecode(buf))

        for _ in range(1, bc.len):
            # consume any additional bytes.
            # this is how IDA knows the size of the insn.
            insn.get_next_byte()

        insn.Op1.type = ida_ua.o_void
        insn.Op2.type = ida_ua.o_void

        # decode instruction operand.
        # as of today (V1), there's at most a single operand.
        # (though there may also be alignment directive, etc. that we place into Op2+)
        #
        # place the operand value into `.value`, unless its a local, and then use `.reg`.
        # use `.specval` to indicate special handling of register, possible cases:
        #   WASM_LOCAL
        #   WASM_GLOBAL
        #   WASM_FUNC_INDEX
        #   WASM_TYPE_INDEX
        #   WASM_BLOCK
        #   WASM_ALIGN
        #
        if bc.imm is not None:
            immtype = bc.imm.get_meta().structure

            SHOW_FLAGS = ida_ua.OF_NO_BASE_DISP | ida_ua.OF_NUMBER | ida_ua.OF_SHOW

            # by default, display the operand, unless overridden below.
            insn.Op1.flags = SHOW_FLAGS

            # block, loop, if
            if immtype == wasm.immtypes.BlockImm:
                # sig = BlockTypeField()
                insn.Op1.type = WASM_BLOCK
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.sig
                insn.Op1.specval = WASM_BLOCK

            # br, br_if
            elif immtype == wasm.immtypes.BranchImm:
                # relative_depth = VarUInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.relative_depth

            # br_table
            elif immtype == wasm.immtypes.BranchTableImm:
                # target_count = VarUInt32Field()
                # target_table = RepeatField(VarUInt32Field(), lambda x: x.target_count)
                # default_target = VarUInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.target_count

                insn.Op2.type = ida_ua.o_imm
                insn.Op2.flags = SHOW_FLAGS
                insn.Op2.dtype = ida_ua.dt_dword
                # save instruction address for rendering branch table
                insn.Op2.addr = insn.ea
                insn.Op2.specval = WASM_BRANCH_TABLE

                insn.Op3.type = ida_ua.o_imm
                insn.Op3.flags = SHOW_FLAGS
                insn.Op3.dtype = ida_ua.dt_dword
                insn.Op3.value = bc.imm.default_target
                insn.Op3.specval = WASM_BRANCH_TABLE_DEFAULT

            # call
            elif immtype == wasm.immtypes.CallImm:
                # function_index = VarUInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.function_index
                insn.Op1.specval = WASM_FUNC_INDEX

            # call_indirect
            elif immtype == wasm.immtypes.CallIndirectImm:
                # type_index = VarUInt32Field()
                # reserved = VarUInt1Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.type_index
                insn.Op1.specval = WASM_TYPE_INDEX

                insn.Op2.type = ida_ua.o_imm
                insn.Op2.flags = SHOW_FLAGS
                insn.Op2.dtype = ida_ua.dt_dword
                insn.Op2.value = bc.imm.reserved

            # get_local, set_local, tee_local
            elif immtype == wasm.immtypes.LocalVarXsImm:
                # local_index = VarUInt32Field()
                insn.Op1.type = ida_ua.o_reg
                insn.Op1.reg = bc.imm.local_index
                insn.Op1.specval = WASM_LOCAL

            # get_global, set_global
            elif immtype == wasm.immtypes.GlobalVarXsImm:
                # global_index = VarUInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.global_index
                insn.Op1.specval = WASM_GLOBAL

            # *.load*, *.store*
            elif immtype == wasm.immtypes.MemoryImm:
                # flags = VarUInt32Field()
                # offset = VarUInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.offset

                insn.Op2.type = ida_ua.o_imm
                insn.Op2.flags = SHOW_FLAGS
                insn.Op2.dtype = ida_ua.dt_dword
                insn.Op2.value = bc.imm.flags
                insn.Op2.specval = WASM_ALIGN

            # current_memory, grow_memory
            elif immtype == wasm.immtypes.CurGrowMemImm:
                # reserved = VarUInt1Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.reserved

            # i32.const
            elif immtype == wasm.immtypes.I32ConstImm:
                # value = VarInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_dword
                insn.Op1.value = bc.imm.value

            # i64.const
            elif immtype == wasm.immtypes.I64ConstImm:
                # value = VarInt64Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_qword
                insn.Op1.value = bc.imm.value

            # f32.const
            elif immtype == wasm.immtypes.F32ConstImm:
                # value = UInt32Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_float
                insn.Op1.value = bc.imm.value

            # f64.const
            elif immtype == wasm.immtypes.F64ConstImm:
                # value = UInt64Field()
                insn.Op1.type = ida_ua.o_imm
                insn.Op1.dtype = ida_ua.dt_double
                insn.Op1.value = bc.imm.value

        return insn.size

    def init_instructions(self):
        # Now create an instruction table compatible with IDA processor module requirements
        self.insns = {}
        for i, op in enumerate(wasm.opcodes.OPCODES):
            self.insns[op.id] = {
                # the opcode byte
                'opcode': op.id,
                # the IDA constant for this instruction
                'id': i,
                'name': op.mnemonic,
                'feature': op.flags,
                'cmt': idawasm.const.WASM_OPCODE_DESCRIPTIONS.get(op.id),
            }
            clean_mnem = op.mnemonic.replace('.', '_').replace('/', '_').upper()
            # the itype constant value must be contiguous, which sucks, because its not the op.id value.
            setattr(self, 'itype_' + clean_mnem, i)

        # Array of instructions
        # the index into this array apparently must match the `self.itype_*`.
        self.instruc = list(sorted(self.insns.values(), key=lambda i: i['id']))

        self.instruc_start = 0
        self.instruc_end = len(self.instruc)
        self.icode_return = self.itype_RETURN

    def init_registers(self):
        """This function parses the register table and creates corresponding ireg_XXX constants"""

        # Registers definition
        # for wasm, "registers" are local variables.
        self.reg_names = []

        # we'd want to scan the module and pick the max number of parameters,
        # however, the data isn't available yet,
        # so we pick a scary large number.
        #
        # note: IDA reg_t size is 16-bits
        MAX_LOCALS = 0x1000
        for i in range(MAX_LOCALS):
            self.reg_names.append("$local%d" % (i))

        # we'd want to scan the module and pick the max number of parameters,
        # however, the data isn't available yet,
        # so we pick a scary large number.
        MAX_PARAMS = 0x1000
        for i in range(MAX_PARAMS):
            self.reg_names.append("$param%d" % (i))

        # these are fake, "virtual" registers.
        # req'd for IDA, apparently.
        # (not actually used in wasm)
        self.reg_names.append("SP")
        self.reg_names.append("CS")
        self.reg_names.append("DS")

        # Create the ireg_XXXX constants.
        # for wasm, will look like: ireg_LOCAL0, ireg_PARAM0
        for i in range(len(self.reg_names)):
            setattr(self, 'ireg_' + self.reg_names[i].replace('$', ''), i)

        # Segment register information (use virtual CS and DS registers if your
        # processor doesn't have segment registers):
        # (not actually used in wasm)
        self.reg_first_sreg = self.ireg_CS
        self.reg_last_sreg = self.ireg_DS

        # number of CS register
        # (not actually used in wasm)
        self.reg_code_sreg = self.ireg_CS

        # number of DS register
        # (not actually used in wasm)
        self.reg_data_sreg = self.ireg_DS

    def __init__(self):
        # this is called prior to loading a binary, so don't read from the database here.
        ida_idp.processor_t.__init__(self)
        self.PTRSZ = 4  # Assume PTRSZ = 4 by default
        self.init_instructions()
        self.init_registers()

        # these will be populated by `notify_newfile`
        self.buf = b''
        # ordered list of wasm section objects
        self.sections: list[ModuleFragment] = []
        # map from function index to function object
        self.functions: dict[int, Function] = {}
        # map from virtual address to function object
        self.function_offsets = {}
        # map from (va-start, va-end) to function object
        self.function_ranges = {}
        # map from global index to global object
        self.globals: dict[int, Global] = {}
        # map from data index to data object
        self.data: dict[int, Data] = {}
        # map from va to map from relative depth to va
        self.branch_targets: dict[int, dict[str, Block]] = {}
        # list of type descriptors
        self.types = []

        # map from address to list of cref arguments.
        # used by `notify_emu`.
        self.deferred_flows = {}

        # set of addresses which should not flow.
        # map from address to True.
        # used by `notify_emu`.
        self.deferred_noflows = {}


def PROCESSOR_ENTRY():
    logging.basicConfig(level=logging.DEBUG)
    return wasm_processor_t()
