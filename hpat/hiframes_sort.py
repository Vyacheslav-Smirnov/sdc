import numpy as np
import math
from collections import namedtuple
import numba
from numba import typeinfer, ir, ir_utils, config, types
from numba.ir_utils import (visit_vars_inner, replace_vars_inner,
                            compile_to_numba_ir, replace_arg_nodes)
from numba.typing import signature
from numba.extending import overload
import hpat
import hpat.timsort
from hpat import distributed, distributed_analysis
from hpat.distributed_api import Reduce_Type
from hpat.distributed_analysis import Distribution
from hpat.utils import debug_prints, empty_like_type
from hpat.str_arr_ext import string_array_type, to_string_list, cp_str_list_to_array, str_list_to_array

MIN_SAMPLES = 1000000
#MIN_SAMPLES = 100
samplePointsPerPartitionHint = 20
MPI_ROOT = 0


class Sort(ir.Stmt):
    def __init__(self, df_in, key_arr, df_vars, loc):
        self.df_in = df_in
        self.df_vars = df_vars
        self.key_arr = key_arr
        self.loc = loc

    def __repr__(self):  # pragma: no cover
        in_cols = ""
        for (c, v) in self.df_vars.items():
            in_cols += "'{}':{}, ".format(c, v.name)
        df_in_str = "{}{{{}}}".format(self.df_in, in_cols)
        return "sort: [key: {}] {}".format(self.key_arr, df_in_str)


def sort_array_analysis(sort_node, equiv_set, typemap, array_analysis):

    # arrays of input df have same size in first dimension as key array
    col_shape = equiv_set.get_shape(sort_node.key_arr)
    if typemap[sort_node.key_arr.name] == string_array_type:
        all_shapes = []
    else:
        all_shapes = [col_shape[0]]
    for col_var in sort_node.df_vars.values():
        typ = typemap[col_var.name]
        if typ == string_array_type:
            continue
        col_shape = equiv_set.get_shape(col_var)
        all_shapes.append(col_shape[0])

    if len(all_shapes) > 1:
        equiv_set.insert_equiv(*all_shapes)

    return [], []


numba.array_analysis.array_analysis_extensions[Sort] = sort_array_analysis


def sort_distributed_analysis(sort_node, array_dists):

    # input columns have same distribution
    in_dist = array_dists[sort_node.key_arr.name]
    for col_var in sort_node.df_vars.values():
        in_dist = Distribution(
            min(in_dist.value, array_dists[col_var.name].value))

    # set dists
    for col_var in sort_node.df_vars.values():
        array_dists[col_var.name] = in_dist
    array_dists[sort_node.key_arr.name] = in_dist
    return


distributed_analysis.distributed_analysis_extensions[Sort] = sort_distributed_analysis

def sort_typeinfer(sort_node, typeinferer):
    # no need for inference since sort just uses arrays without creating any
    return

typeinfer.typeinfer_extensions[Sort] = sort_typeinfer


def visit_vars_sort(sort_node, callback, cbdata):
    if debug_prints():  # pragma: no cover
        print("visiting sort vars for:", sort_node)
        print("cbdata: ", sorted(cbdata.items()))

    sort_node.key_arr = visit_vars_inner(
        sort_node.key_arr, callback, cbdata)

    for col_name in list(sort_node.df_vars.keys()):
        sort_node.df_vars[col_name] = visit_vars_inner(
            sort_node.df_vars[col_name], callback, cbdata)

# add call to visit sort variable
ir_utils.visit_vars_extensions[Sort] = visit_vars_sort


def remove_dead_sort(sort_node, lives, arg_aliases, alias_map, func_ir, typemap):
    #
    dead_cols = []

    for col_name, col_var in sort_node.df_vars.items():
        if col_var.name not in lives:
            dead_cols.append(col_name)

    for cname in dead_cols:
        sort_node.df_vars.pop(cname)

    # remove empty sort node
    if len(sort_node.df_vars) == 0 and sort_node.key_arr.name not in lives:
        return None

    return sort_node


ir_utils.remove_dead_extensions[Sort] = remove_dead_sort


def sort_usedefs(sort_node, use_set=None, def_set=None):
    if use_set is None:
        use_set = set()
    if def_set is None:
        def_set = set()

    # key array and input columns are used
    use_set.add(sort_node.key_arr.name)
    use_set.update({v.name for v in sort_node.df_vars.values()})

    return numba.analysis._use_defs_result(usemap=use_set, defmap=def_set)


numba.analysis.ir_extension_usedefs[Sort] = sort_usedefs


def get_copies_sort(sort_node, typemap):
    # sort doesn't generate copies
    return set(), set()

ir_utils.copy_propagate_extensions[Sort] = get_copies_sort


def apply_copies_sort(sort_node, var_dict, name_var_table,
                        typemap, calltypes, save_copies):
    """apply copy propagate in sort node"""
    sort_node.key_arr = replace_vars_inner(sort_node.key_arr, var_dict)

    for col_name in list(sort_node.df_vars.keys()):
        sort_node.df_vars[col_name] = replace_vars_inner(
            sort_node.df_vars[col_name], var_dict)

    return

ir_utils.apply_copy_propagate_extensions[Sort] = apply_copies_sort

def to_string_list_typ(typ):
    if typ == string_array_type:
        return types.List(hpat.str_ext.string_type)

    if isinstance(typ, (types.Tuple, types.UniTuple)):
        new_typs = []
        for i in range(typ.count):
            new_typs.append(to_string_list_typ(typ.types[i]))
        return types.Tuple(new_typs)

    return typ

def sort_distributed_run(sort_node, array_dists, typemap, calltypes, typingctx, targetctx):
    parallel = True
    data_vars = list(sort_node.df_vars.values())
    for v in [sort_node.key_arr] + data_vars:
        if (array_dists[v.name] != distributed.Distribution.OneD
                and array_dists[v.name] != distributed.Distribution.OneD_Var):
            parallel = False

    key_arr = sort_node.key_arr
    key_typ = typemap[key_arr.name]
    data_tup_typ = types.Tuple([typemap[v.name] for v in sort_node.df_vars.values()])

    sort_state_spec = [
        ('key_arr', to_string_list_typ(key_typ)),
        ('aLength', numba.intp),
        ('minGallop', numba.intp),
        ('tmpLength', numba.intp),
        ('tmp', to_string_list_typ(key_typ)),
        ('stackSize', numba.intp),
        ('runBase', numba.int64[:]),
        ('runLen', numba.int64[:]),
        ('data', to_string_list_typ(data_tup_typ)),
        ('tmp_data', to_string_list_typ(data_tup_typ)),
    ]

    col_name_args = ', '.join(["c"+str(i) for i in range(len(data_vars))])
    # TODO: use *args
    func_text = "def f(key_arr, {}):\n".format(col_name_args)
    func_text += "  data = ({}{})\n".format(col_name_args,
        "," if len(data_vars) == 1 else "")  # single value needs comma to become tuple
    func_text += "  _sort_len = len(key_arr)\n"
    # convert StringArray to list(string) to enable swapping in sort
    func_text += "  l_key_arr = to_string_list(key_arr)\n"
    func_text += "  l_data = to_string_list(data)\n"
    func_text += "  sort_state = SortState(l_key_arr, _sort_len, l_data)\n"
    func_text += "  hpat.timsort.sort(sort_state, l_key_arr, 0, _sort_len, l_data)\n"
    func_text += "  cp_str_list_to_array(key_arr, l_key_arr)\n"
    func_text += "  cp_str_list_to_array(data, l_data)\n"

    loc_vars = {}
    exec(func_text, {}, loc_vars)
    sort_impl = loc_vars['f']

    SortStateCL = numba.jitclass(sort_state_spec)(hpat.timsort.SortState)

    f_block = compile_to_numba_ir(sort_impl,
                                    {'hpat': hpat, 'SortState': SortStateCL,
                                    'to_string_list': to_string_list,
                                    'cp_str_list_to_array': cp_str_list_to_array},
                                    typingctx,
                                    tuple([key_typ] + list(data_tup_typ.types)),
                                    typemap, calltypes).blocks.popitem()[1]
    replace_arg_nodes(f_block, [sort_node.key_arr] + data_vars)
    nodes = f_block.body[:-3]

    if not parallel:
        return nodes

    # parallel case
    # TODO: refactor with previous call, use *args?
    # get data variable tuple
    func_text = "def f({}):\n".format(col_name_args)
    func_text += "  data = ({}{})\n".format(col_name_args,
        "," if len(data_vars) == 1 else "")  # single value needs comma to become tuple

    loc_vars = {}
    exec(func_text, {}, loc_vars)
    tup_impl = loc_vars['f']
    f_block = compile_to_numba_ir(tup_impl,
                                    {},
                                    typingctx,
                                    list(data_tup_typ.types),
                                    typemap, calltypes).blocks.popitem()[1]

    replace_arg_nodes(f_block, data_vars)
    nodes += f_block.body[:-3]
    data_tup_var = nodes[-1].target

    def par_sort_impl(key_arr, data):
        out, out_data = parallel_sort(key_arr, data)
        key_arr = out
        data = out_data
        l_key_arr = to_string_list(key_arr)
        # TODO: use k-way merge instead of sort
        # sort output
        n_out = len(key_arr)
        sort_state_o = SortState(l_key_arr, n_out, data)
        hpat.timsort.sort(sort_state_o, l_key_arr, 0, n_out, data)
        cp_str_list_to_array(key_arr, l_key_arr)

    f_block = compile_to_numba_ir(par_sort_impl,
                                    {'hpat': hpat, 'SortState': SortStateCL,
                                    'parallel_sort': parallel_sort,
                                    'to_string_list': to_string_list,
                                    'cp_str_list_to_array': cp_str_list_to_array},
                                    typingctx,
                                    (key_typ, data_tup_typ),
                                    typemap, calltypes).blocks.popitem()[1]
    replace_arg_nodes(f_block, [sort_node.key_arr, data_tup_var])
    nodes += f_block.body[:-3]
    return nodes


distributed.distributed_run_extensions[Sort] = sort_distributed_run


@numba.njit
def parallel_sort(key_arr, data):
    n_local = len(key_arr)
    n_total = hpat.distributed_api.dist_reduce(n_local, np.int32(Reduce_Type.Sum.value))

    n_pes = hpat.distributed_api.get_size()
    my_rank = hpat.distributed_api.get_rank()

    # similar to Spark's sample computation Partitioner.scala
    sampleSize = min(samplePointsPerPartitionHint * n_pes, MIN_SAMPLES)

    fraction = min(sampleSize / max(n_total, 1), 1.0)
    n_loc_samples = min(math.ceil(fraction * n_local), n_local)
    inds = np.random.randint(0, n_local, n_loc_samples)
    samples = key_arr[inds]
    # print(sampleSize, fraction, n_local, n_loc_samples, len(samples))

    all_samples = hpat.distributed_api.gatherv(samples)
    all_samples = to_string_list(all_samples)
    bounds = empty_like_type(n_pes-1, all_samples)

    if my_rank == MPI_ROOT:
        all_samples.sort()
        n_samples = len(all_samples)
        step = math.ceil(n_samples / n_pes)
        for i in range(n_pes - 1):
            bounds[i] = all_samples[min((i + 1) * step, n_samples - 1)]
        # print(bounds)

    bounds = str_list_to_array(bounds)
    bounds = hpat.distributed_api.prealloc_str_for_bcast(bounds)
    hpat.distributed_api.bcast(bounds)

    # calc send/recv counts
    shuffle_meta = alloc_shuffle_metadata(key_arr, n_pes)
    node_id = 0
    for i in range(n_local):
        if node_id < (n_pes - 1) and key_arr[i] >= bounds[node_id]:
            node_id += 1
        update_shuffle_meta(shuffle_meta, node_id)

    finalize_shuffle_meta(key_arr, shuffle_meta)

    # shuffle
    alltoallv(key_arr, shuffle_meta)
    out_data = hpat.timsort.alloc_arr_tup(shuffle_meta.n_out, data)
    hpat.distributed_api.alltoallv_tup(data, out_data,
        shuffle_meta.send_counts, shuffle_meta.recv_counts, shuffle_meta.send_disp, shuffle_meta.recv_disp)

    return shuffle_meta.out_arr, out_data

# ShuffleMeta = namedtuple('ShuffleMeta',
#     ['send_counts', 'recv_counts', 'out_arr', 'n_out', 'send_disp', 'recv_disp', 'send_counts_char',
#     'recv_counts_char', 'send_arr_lens', 'send_arr_chars'])

class ShuffleMeta:
    def __init__(self, send_counts, recv_counts, out_arr, n_out, send_disp, recv_disp, send_counts_char,
            recv_counts_char, send_arr_lens, send_arr_chars):
        self.send_counts = send_counts
        self.recv_counts = recv_counts
        self.out_arr = out_arr
        self.n_out = n_out
        self.send_disp = send_disp
        self.recv_disp = recv_disp
        self.send_counts_char = send_counts_char
        self.recv_counts_char = recv_counts_char
        self.send_arr_lens = send_arr_lens
        self.send_arr_chars = send_arr_chars

@numba.njit
def update_shuffle_meta(shuffle_meta, node_id):
    shuffle_meta.send_counts[node_id] += 1

def alloc_shuffle_metadata(arr, n_pes):
    return ShuffleMeta(arr, n_pes)

@overload(alloc_shuffle_metadata)
def alloc_shuffle_metadata_overload(arr_t, n_pes_t):
    count_arr_typ = types.Array(types.int32, 1, 'C')
    if isinstance(arr_t, types.Array):
        spec = [
            ('send_counts', count_arr_typ),
            ('recv_counts', count_arr_typ),
            ('out_arr', arr_t),
            ('n_out', types.intp),
            ('send_disp', count_arr_typ),
            ('recv_disp', count_arr_typ),
            ('send_counts_char', types.none),
            ('recv_counts_char', types.none),
            ('send_arr_lens', types.none),
            ('send_arr_chars', types.none)
        ]
        ShuffleMetaCL = numba.jitclass(spec)(ShuffleMeta)
        def shuff_meta_impl(arr, n_pes):
            send_counts = np.zeros(n_pes, np.int32)
            recv_counts = np.empty(n_pes, np.int32)
            # arr as out_arr placeholder, send/recv counts as placeholder for type inference
            return ShuffleMetaCL(send_counts, recv_counts, arr, 0, send_counts, recv_counts, None, None, None, None)
        return shuff_meta_impl

def finalize_shuffle_meta(arr, shuffle_meta):
    return

@overload(finalize_shuffle_meta)
def finalize_shuffle_meta_overload(arr_t, shuffle_meta_t):
    if isinstance(arr_t, types.Array):
        def finalize_impl(arr, shuffle_meta):
            hpat.distributed_api.alltoall(shuffle_meta.send_counts, shuffle_meta.recv_counts, 1)
            shuffle_meta.n_out = shuffle_meta.recv_counts.sum()
            shuffle_meta.out_arr = np.empty(shuffle_meta.n_out, arr.dtype)
            shuffle_meta.send_disp = hpat.hiframes_join.calc_disp(shuffle_meta.send_counts)
            shuffle_meta.recv_disp = hpat.hiframes_join.calc_disp(shuffle_meta.recv_counts)
        return finalize_impl


def alltoallv(arr, m):
    return

@overload(alltoallv)
def alltoallv_impl(arr_t, metadata_t):
    if isinstance(arr_t, types.Array):
        def a2av_impl(arr, metadata):
            hpat.distributed_api.alltoallv(
                arr, metadata.out_arr, metadata.send_counts,
                metadata.recv_counts, metadata.send_disp, metadata.recv_disp)
    return a2av_impl
