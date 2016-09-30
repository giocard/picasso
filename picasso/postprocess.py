"""
    gui/postprocess
    ~~~~~~~~~~~~~~~~~~~~

    Data analysis of localization lists

    :author: Joerg Schnitzbauer, 2015
    :copyright: Copyright (c) 2015 Jungmann Lab, Max Planck Institute of Biochemistry
"""

import numpy as _np
import numba as _numba
from sklearn.cluster import DBSCAN as _DBSCAN
from tqdm import tqdm as _tqdm
from tqdm import trange as _trange
from scipy import interpolate as _interpolate
from scipy.special import iv as _iv
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
import multiprocessing as _multiprocessing
import matplotlib.pyplot as _plt
import itertools as _itertools
import lmfit as _lmfit
from collections import OrderedDict as _OrderedDict
from . import io as _io
from . import lib as _lib
from . import render as _render
from . import imageprocess as _imageprocess
from threading import Thread as _Thread
import time as _time


def get_index_blocks(locs, info, size, callback=None):
    # Sort locs by indices
    x_index = _np.uint32(locs.x / size)
    y_index = _np.uint32(locs.y / size)
    sort_indices = _np.lexsort([x_index, y_index])
    locs = locs[sort_indices]
    x_index = x_index[sort_indices]
    y_index = y_index[sort_indices]
    # Allocate block info arrays
    n_blocks_y, n_blocks_x = index_blocks_shape(info, size)
    block_starts = _np.zeros((n_blocks_y, n_blocks_x), dtype=_np.uint32)
    block_ends = _np.zeros((n_blocks_y, n_blocks_x), dtype=_np.uint32)
    K, L = block_starts.shape
    # Fill in block starts and ends
    # We are running this in a thread with a nogil numba function. This helps updating a potential GUI with the callback.
    if callback is not None:
        callback(0)
        counter = [0]
    else:
        counter = None
    thread = _Thread(target=_fill_index_blocks, args=(block_starts, block_ends, x_index, y_index, counter))
    thread.start()
    if callback is not None:
        while counter[0] < K:
            callback(counter[0])
            _time.sleep(0.1)
    thread.join()
    if callback is not None:
        callback(counter[0])
    return locs, size, x_index, y_index, block_starts, block_ends, K, L


def index_blocks_shape(info, size):
    n_blocks_x = int(_np.ceil(info[0]['Width'] / size))
    n_blocks_y = int(_np.ceil(info[0]['Height'] / size))
    return n_blocks_y, n_blocks_x


@_numba.jit(nopython=True, nogil=True)
def n_block_locs_at(x, y, size, K, L, block_starts, block_ends):
    x_index = _np.uint32(x / size)
    y_index = _np.uint32(y / size)
    n_block_locs = 0
    for k in range(y_index - 1, y_index + 2):
        if 0 < k < K:
            for l in range(x_index - 1, x_index + 2):
                if 0 < l < L:
                    n_block_locs += block_ends[k, l] - block_starts[k, l]
    return n_block_locs


def get_block_locs_at(x, y, index_blocks):
    locs, size, x_index, y_index, block_starts, block_ends, K, L = index_blocks
    x_index = _np.uint32(x / size)
    y_index = _np.uint32(y / size)
    indices = []
    for k in range(y_index - 1, y_index+2):
        if 0 < k < K:
            for l in range(x_index - 1, x_index + 2):
                if 0 < l < L:
                    indices.append(list(range(block_starts[k, l], block_ends[k, l])))
    indices = list(_itertools.chain(*indices))
    return locs[indices]


def _fill_index_blocks(block_starts, block_ends, x_index, y_index, counter=None):
    Y, X = block_starts.shape
    N = len(x_index)
    k = 0
    if counter is not None:
        counter[0] = 0
    for i in range(Y):
        for j in range(X):
            k = _fill_index_block(block_starts, block_ends, N, x_index, y_index, i, j, k)
        if counter is not None:
            counter[0] = i+1


@_numba.jit(nopython=True, nogil=True)
def _fill_index_block(block_starts, block_ends, N, x_index, y_index, i, j, k):
    block_starts[i, j] = k
    while k < N and y_index[k] == i and x_index[k] == j:
        k += 1
    block_ends[i, j] = k
    return k


@_numba.jit(nopython=True, nogil=True)
def _distance_histogram(locs, bin_size, r_max, x_index, y_index, block_starts, block_ends, start, chunk):
    x = locs.x
    y = locs.y
    dh_len = _np.uint32(r_max / bin_size)
    dh = _np.zeros(dh_len, dtype=_np.uint32)
    r_max_2 = r_max**2
    K, L = block_starts.shape
    end = min(start+chunk, len(locs))
    for i in range(start, end):
        xi = x[i]
        yi = y[i]
        ki = y_index[i]
        li = x_index[i]
        for k in range(ki, ki+2):
            if k < K:
                for l in range(li, li+2):
                    if l < L:
                        for j in range(block_starts[k, l], block_ends[k, l]):
                            if j > i:
                                dx2 = (xi - x[j])**2
                                if dx2 < r_max_2:
                                    dy2 = (yi - y[j])**2
                                    if dy2 < r_max_2:
                                        d = _np.sqrt(dx2 + dy2)
                                        if d < r_max:
                                            bin = _np.uint32(d / bin_size)
                                            if bin < dh_len:
                                                dh[bin] += 1
    return dh


def distance_histogram(locs, info, bin_size, r_max):
    locs, x_index, y_index, block_starts, block_ends = get_index_blocks(locs, info, r_max)
    N = len(locs)
    n_threads = _multiprocessing.cpu_count()
    chunk = int(N / n_threads)
    starts = range(0, N, chunk)
    args = [(locs, bin_size, r_max, x_index, y_index, block_starts, block_ends, start, chunk) for start in starts]
    with _ThreadPoolExecutor() as executor:
        futures = [executor.submit(_distance_histogram, *_) for _ in args]
    results = [future.result() for future in futures]
    return _np.sum(results, axis=0)


def nena(locs, info, callback=None):
    bin_centers, dnfl_ = next_frame_neighbor_distance_histogram(locs, callback)

    def func(d, a, s, ac, dc, sc):
        f = a * (d / s**2) * _np.exp(-0.5 * d**2 / s**2)
        fc = ac * (d / sc**2) * _np.exp(-0.5 * (d**2 + dc**2) / sc**2) * _iv(0, d * dc / sc)
        return f + fc

    pdf_model = _lmfit.Model(func)
    params = _lmfit.Parameters()
    area = _np.trapz(dnfl_, bin_centers)
    median_lp = _np.mean([_np.median(locs.lpx), _np.median(locs.lpy)])
    params.add('a', value=area/2, min=0)
    params.add('s', value=median_lp, min=0)
    params.add('ac', value=area/2, min=0)
    params.add('dc', value=2*median_lp, min=0)
    params.add('sc', value=median_lp, min=0)
    result = pdf_model.fit(dnfl_, params, d=bin_centers)
    return result, result.best_values['s']


def next_frame_neighbor_distance_histogram(locs, callback=None):
    locs.sort(kind='mergesort', order='frame')
    frame = locs.frame
    x = locs.x
    y = locs.y
    if hasattr(locs, 'group'):
        group = locs.group
    else:
        group = _np.zeros(len(locs), dtype=_np.int32)
    bin_size = 0.001
    d_max = 1.0
    return _nfndh(frame, x, y, group, d_max, bin_size, callback)


def _nfndh(frame, x, y, group, d_max, bin_size, callback=None):
    N = len(frame)
    bins = _np.arange(0, d_max, bin_size)
    dnfl = _np.zeros(len(bins))
    if callback is not None:
        callback(0)
    for i in range(N):
        _fill_dnfl(N, frame, x, y, group, i, d_max, dnfl, bin_size)
        if callback is not None:
            callback(i+1)
    bin_centers = bins + bin_size / 2
    return bin_centers, dnfl


@_numba.jit(nopython=True)
def _fill_dnfl(N, frame, x, y, group, i, d_max, dnfl, bin_size):
    frame_i = frame[i]
    x_i = x[i]
    y_i = y[i]
    group_i = group[i]
    min_frame = frame_i + 1
    for min_index in range(i + 1, N):
        if frame[min_index] >= min_frame:
            break
    max_frame = frame_i + 1
    for max_index in range(min_index, N):
        if frame[max_index] > max_frame:
            break
    d_max_2 = d_max**2
    for j in range(min_index, max_index):
        if group[j] == group_i:
            dx2 = (x_i - x[j])**2
            if dx2 <= d_max_2:
                dy2 = (y_i - y[j])**2
                if dy2 <= d_max_2:
                    d = _np.sqrt(dx2 + dy2)
                    if d <= d_max:
                        bin = int(d / bin_size)
                        dnfl[bin] += 1


def pair_correlation(locs, info, bin_size, r_max):
    dh = distance_histogram(locs, info, bin_size, r_max)
    bins_lower = _np.arange(0, r_max, bin_size)
    area = _np.pi * bin_size * (2 * bins_lower + bin_size)
    return bins_lower, dh / area


def dbscan(locs, radius, min_density):
    print('Identifying clusters...')
    locs = locs[_np.isfinite(locs.x) & _np.isfinite(locs.y)]
    X = _np.vstack((locs.x, locs.y)).T
    db = _DBSCAN(eps=radius, min_samples=min_density).fit(X)
    group = _np.int32(db.labels_)       # int32 for Origin compatiblity
    locs = _lib.append_to_rec(locs, group, 'group')
    locs = locs[locs.group != -1]
    print('Generating cluster information...')
    groups = _np.unique(locs.group)
    n_groups = len(groups)
    mean_frame = _np.zeros(n_groups)
    std_frame = _np.zeros(n_groups)
    com_x = _np.zeros(n_groups)
    com_y = _np.zeros(n_groups)
    std_x = _np.zeros(n_groups)
    std_y = _np.zeros(n_groups)
    n = _np.zeros(n_groups, dtype=_np.int32)
    for i, group in enumerate(groups):
        group_locs = locs[locs.group == i]
        mean_frame[i] = _np.mean(group_locs.frame)
        com_x[i] = _np.mean(group_locs.x)
        com_y[i] = _np.mean(group_locs.y)
        std_frame[i] = _np.std(group_locs.frame)
        std_x[i] = _np.std(group_locs.x)
        std_y[i] = _np.std(group_locs.y)
        n[i] = len(group_locs)
    clusters = _np.rec.array((groups, mean_frame, com_x, com_y, std_frame, std_x, std_y, n),
                             dtype=[('groups', groups.dtype), ('mean_frame', 'f4'), ('com_x', 'f4'), ('com_y', 'f4'),
                             ('std_frame', 'f4'), ('std_x', 'f4'), ('std_y', 'f4'), ('n', 'i4')])
    return clusters, locs


@_numba.jit(nopython=True, nogil=True)
def _local_density(locs, radius, x_index, y_index, block_starts, block_ends, start, chunk):
    x = locs.x
    y = locs.y
    N = len(x)
    r2 = radius**2
    end = min(start+chunk, N)
    density = _np.zeros(N, dtype=_np.uint32)
    for i in range(start, end):
        yi = y[i]
        xi = x[i]
        ki = y_index[i]
        li = x_index[i]
        di = 0
        for k in range(ki-1, ki+2):
            for l in range(li-1, li+2):
                j_min = block_starts[k, l]
                j_max = block_ends[k, l]
                for j in range(j_min, j_max):
                    dx2 = (xi - x[j])**2
                    if dx2 < r2:
                        dy2 = (yi - y[j])**2
                        if dy2 < r2:
                            d2 = dx2 + dy2
                            if d2 < r2:
                                di += 1
        density[i] = di
    return density


def compute_local_density(locs, info, radius):
    locs, x_index, y_index, block_starts, block_ends = get_index_blocks(locs, info, radius)
    N = len(locs)
    n_threads = _multiprocessing.cpu_count()
    chunk = int(N / n_threads)
    starts = range(0, N, chunk)
    args = [(locs, radius, x_index, y_index, block_starts, block_ends, start, chunk) for start in starts]
    with _ThreadPoolExecutor() as executor:
        futures = [executor.submit(_local_density, *_) for _ in args]
    density = _np.sum([future.result() for future in futures], axis=0)
    locs = _lib.remove_from_rec(locs, 'density')
    return _lib.append_to_rec(locs, density, 'density')


def compute_dark_times(locs, group=None):
    dark = dark_times(locs, group)
    locs = _lib.append_to_rec(locs, _np.int32(dark), 'dark')
    return locs


def dark_times(locs, group=None, invalid=True):
    last_frame = locs.frame + locs.len - 1
    if group is None:
        if hasattr(locs, 'group'):
            group = locs.group
        else:
            group = _np.zeros(len(locs))
    dark = _dark_times(locs, group, last_frame)
    if not invalid:
        dark = dark[dark != -1]
    return dark


@_numba.jit(nopython=True)
def _dark_times(locs, group, last_frame):
    N = len(locs)
    max_frame = locs.frame.max()
    dark = max_frame * _np.ones(len(locs), dtype=_np.int32)
    for i in range(N):
        for j in range(N):
            if (group[i] == group[j]) and (i != j):
                dark_ij = locs.frame[i] - last_frame[j]
                if (dark_ij > 0) and (dark_ij < dark[i]):
                    dark[i] = dark_ij
    for i in range(N):
        if dark[i] == max_frame:
            dark[i] = -1
    return dark


def link(locs, info, r_max=0.05, max_dark_time=1, combine_mode='average'):
    if len(locs) == 0:
        linked_locs = locs.copy()
        if hasattr(locs, 'frame'):
            linked_locs = _lib.append_to_rec(linked_locs, _np.array([], dtype=_np.int32), 'len')
            linked_locs = _lib.append_to_rec(linked_locs, _np.array([], dtype=_np.int32), 'n')
        if hasattr(locs, 'photons'):
            linked_locs = _lib.append_to_rec(linked_locs, _np.array([], dtype=_np.float32), 'photon_rate')
    else:
        locs.sort(kind='mergesort', order='frame')
        if hasattr(locs, 'group'):
            group = locs.group
        else:
            group = _np.zeros(len(locs), dtype=_np.int32)
        link_group = get_link_groups(locs, r_max, max_dark_time, group)
        if combine_mode == 'average':
            linked_locs = link_loc_groups(locs, link_group)
        elif combine_mode == 'refit':
            pass    # TODO
    return linked_locs


@_numba.jit(nopython=True)
def get_link_groups(locs, d_max, max_dark_time, group):
    ''' Assumes that locs are sorted by frame '''
    frame = locs.frame
    x = locs.x
    y = locs.y
    N = len(x)
    link_group = -_np.ones(N, dtype=_np.int32)
    current_link_group = -1
    for i in range(N):
        if link_group[i] == -1:  # loc has no group yet
            current_link_group += 1
            link_group[i] = current_link_group
            current_index = i
            next_loc_index_in_group = _get_next_loc_index_in_link_group(current_index, link_group, N, frame, x, y, d_max, max_dark_time, group)
            while next_loc_index_in_group != -1:
                link_group[next_loc_index_in_group] = current_link_group
                current_index = next_loc_index_in_group
                next_loc_index_in_group = _get_next_loc_index_in_link_group(current_index, link_group, N, frame, x, y, d_max, max_dark_time, group)
    return link_group


@_numba.jit(nopython=True)
def _get_next_loc_index_in_link_group(current_index, link_group, N, frame, x, y, d_max, max_dark_time, group):
    current_frame = frame[current_index]
    current_x = x[current_index]
    current_y = y[current_index]
    current_group = group[current_index]
    min_frame = current_frame + 1
    for min_index in range(current_index + 1, N):
        if frame[min_index] >= min_frame:
            break
    max_frame = current_frame + max_dark_time + 1
    for max_index in range(min_index, N):
        if frame[max_index] > max_frame:
            break
    d_max_2 = d_max**2
    for j in range(min_index, max_index):
        if group[j] == current_group:
            if link_group[j] == -1:
                dx2 = (current_x - x[j])**2
                if dx2 <= d_max_2:
                    dy2 = (current_y - y[j])**2
                    if dy2 <= d_max_2:
                        if _np.sqrt(dx2 + dy2) <= d_max:
                            return j
    return -1


@_numba.jit(nopython=True)
def _link_group_count(link_group, n_locs, n_groups):
    result = _np.zeros(n_groups, dtype=_np.uint32)
    for i in range(n_locs):
        i_ = link_group[i]
        result[i_] += 1
    return result


@_numba.jit(nopython=True)
def _link_group_sum(column, link_group, n_locs, n_groups):
    result = _np.zeros(n_groups, dtype=column.dtype)
    for i in range(n_locs):
        i_ = link_group[i]
        result[i_] += column[i]
    return result


@_numba.jit(nopython=True)
def _link_group_mean(column, link_group, n_locs, n_groups, n_locs_per_group):
    group_sum = _link_group_sum(column, link_group, n_locs, n_groups)
    result = _np.empty(n_groups, dtype=_np.float32)     # this ensures float32 after the division
    result[:] = group_sum / n_locs_per_group
    return result


@_numba.jit(nopython=True)
def _link_group_weighted_mean(column, weights, link_group, n_locs, n_groups, n_locs_per_group):
    sum_weights = _link_group_sum(weights, link_group, n_locs, n_groups)
    return _link_group_mean(column * weights, link_group, n_locs, n_groups, sum_weights), sum_weights


@_numba.jit(nopython=True)
def _link_group_min_max(column, link_group, n_locs, n_groups):
    min_ = _np.empty(n_groups, dtype=column.dtype)
    max_ = _np.empty(n_groups, dtype=column.dtype)
    min_[:] = column.max()
    max_[:] = column.min()
    for i in range(n_locs):
        i_ = link_group[i]
        value = column[i]
        if value < min_[i_]:
            min_[i_] = value
        if value > max_[i_]:
            max_[i_] = value
    return min_, max_


@_numba.jit(nopython=True)
def _link_group_last(column, link_group, n_locs, n_groups):
    result = _np.zeros(n_groups, dtype=column.dtype)
    for i in range(n_locs):
        i_ = link_group[i]
        result[i_] = column[i]
    return result


def link_loc_groups(locs, link_group):
    n_locs = len(link_group)
    n_groups = link_group.max() + 1
    n_ = _link_group_count(link_group, n_locs, n_groups)
    columns = _OrderedDict()
    if hasattr(locs, 'frame'):
        first_frame_, last_frame_ = _link_group_min_max(locs.frame, link_group, n_locs, n_groups)
        columns['frame'] = first_frame_
    if hasattr(locs, 'x'):
        weights_x = 1 / locs.lpx**2
        columns['x'], sum_weights_x_ = _link_group_weighted_mean(locs.x, weights_x, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'y'):
        weights_y = 1 / locs.lpy**2
        columns['y'], sum_weights_y_ = _link_group_weighted_mean(locs.y, weights_y, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'photons'):
        columns['photons'] = _link_group_sum(locs.photons, link_group, n_locs, n_groups)
    if hasattr(locs, 'sx'):
        columns['sx'] = _link_group_mean(locs.sx, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'sy'):
        columns['sy'] = _link_group_mean(locs.sy, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'bg'):
        columns['bg'] = _link_group_sum(locs.bg, link_group, n_locs, n_groups)
    if hasattr(locs, 'x'):
        columns['lpx'] = _np.sqrt(1 / sum_weights_x_)
    if hasattr(locs, 'y'):
        columns['lpy'] = _np.sqrt(1 / sum_weights_y_)
    if hasattr(locs, 'net_gradient'):
        columns['net_gradient'] = _link_group_mean(locs.net_gradient, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'likelihood'):
        columns['likelihood'] = _link_group_mean(locs.likelihood, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'iterations'):
        columns['iterations'] = _link_group_mean(locs.iterations, link_group, n_locs, n_groups, n_)
    if hasattr(locs, 'group'):
        columns['group'] = _link_group_last(locs.group, link_group, n_locs, n_groups)
    if hasattr(locs, 'frame'):
        columns['len'] = last_frame_ - first_frame_ + 1
    columns['n'] = n_
    if hasattr(locs, 'photons'):
        columns['photon_rate'] = _np.float32(columns['photons'] / n_)
    return _np.rec.array(list(columns.values()), names=list(columns.keys()))


def undrift(locs, info, segmentation, display=True, segmentation_callback=None, rcc_callback=None):
    bounds, segments = _render.segment(locs, info, segmentation, {'blur_method': 'smooth'}, segmentation_callback)
    shift_y, shift_x = _imageprocess.rcc(segments, 32, rcc_callback)
    t = (bounds[1:] + bounds[:-1]) / 2
    drift_x_pol = _interpolate.InterpolatedUnivariateSpline(t, shift_x, k=3)
    drift_y_pol = _interpolate.InterpolatedUnivariateSpline(t, shift_y, k=3)
    t_inter = _np.arange(info[0]['Frames'])
    drift = (drift_x_pol(t_inter), drift_y_pol(t_inter))
    drift = _np.rec.array(drift, dtype=[('x', 'f'), ('y', 'f')])
    if display:
        _plt.figure(figsize=(17, 6))
        _plt.suptitle('Estimated drift')
        _plt.subplot(1, 2, 1)
        _plt.plot(drift.x, label='x interpolated')
        _plt.plot(drift.y, label='y interpolated')
        t = (bounds[1:] + bounds[:-1]) / 2
        _plt.plot(t, shift_x, 'o', color=list(_plt.rcParams['axes.prop_cycle'])[0]['color'], label='x')
        _plt.plot(t, shift_y, 'o', color=list(_plt.rcParams['axes.prop_cycle'])[1]['color'], label='y')
        _plt.legend(loc='best')
        _plt.xlabel('Frame')
        _plt.ylabel('Drift (pixel)')
        _plt.subplot(1, 2, 2)
        _plt.plot(drift.x, drift.y, color=list(_plt.rcParams['axes.prop_cycle'])[2]['color'])
        _plt.plot(shift_x, shift_y, 'o', color=list(_plt.rcParams['axes.prop_cycle'])[2]['color'])
        _plt.axis('equal')
        _plt.xlabel('x')
        _plt.ylabel('y')
        _plt.show()
    locs.x -= drift.x[locs.frame]
    locs.y -= drift.y[locs.frame]
    return drift, locs


def align(locs, infos, display=False):
    images = []
    for i, (locs_, info_) in enumerate(zip(locs, infos)):
        _, image = _render.render(locs_, info_, blur_method='smooth')
        images.append(image)
    shift_y, shift_x = _imageprocess.rcc(images)
    print('Image x shifts: {}'.format(shift_x))
    print('Image y shifts: {}'.format(shift_y))
    for i, (locs_, dx, dy) in enumerate(zip(locs, shift_x, shift_y)):
        locs_.y -= dy
        locs_.x -= dx
    return locs


def groupprops(locs):
    try:
        locs = locs[locs.dark != -1]
    except AttributeError:
        pass
    group_ids = _np.unique(locs.group)
    n = len(group_ids)
    n_cols = len(locs.dtype)
    names = ['group', 'n_events'] + list(_itertools.chain(*[(_ + '_mean', _ + '_std') for _ in locs.dtype.names]))
    formats = ['i4', 'i4'] + 2 * n_cols * ['f4']
    groups = _np.recarray(n, formats=formats, names=names)
    for i, group_id in enumerate(group_ids):
        group_locs = locs[locs.group == group_id]
        groups['group'][i] = group_id
        groups['n_events'][i] = len(group_locs)
        for name in locs.dtype.names:
            groups[name + '_mean'][i] = _np.mean(group_locs[name])
            groups[name + '_std'][i] = _np.std(group_locs[name])
    return groups


def average(locs, info, iterations=50, oversampling=20, path_basename=None):
    n_digits = len(str(iterations))
    groups = _np.unique(locs.group)
    n_groups = len(groups)
    print('Indexing particle localizations...')
    group_index = [(locs.group == _) for _ in groups]

    # Translate all the groups by center of mass
    for index in _tqdm(group_index, desc='Aligning by COM', unit='groups'):
        locs.x[index] -= _np.mean(locs.x[index])
        locs.y[index] -= _np.mean(locs.y[index])

    r = 2 * _np.sqrt(_np.mean(locs.x**2 + locs.y**2))
    a_step = _np.arcsin(1 / (oversampling * r))
    angles = _np.arange(0, 2*_np.pi, a_step)
    kwargs = {'oversampling': oversampling,
              'viewport': [(-r, -r), (r, r)],
              'blur_method': 'smooth'}
    Y_half = info[0]['Height'] / 2
    X_half = info[0]['Width'] / 2

    print('# Particles:', n_groups)
    print('Super-Resolution Pixel Size: {:.3f} cam. pixels'.format(1 / oversampling))
    print('Translation Range: {:.3f} cam. pixels'.format(r))
    print('Angle Step: {:.3f} degrees'.format(a_step * 360 / (2 * _np.pi)))

    def save(n):
        if path_basename is not None:
            locs.x += X_half
            locs.y += Y_half
            _io.save_locs(path_basename + '_{:0{nd}d}.hdf5'.format(n, nd=n_digits), locs, info)
            locs.x -= X_half
            locs.y -= Y_half
            _plt.imsave(path_basename + '_{:0{nd}d}.png'.format(n, nd=n_digits),
                        image_avg,
                        cmap='magma',
                        vmin=0,
                        vmax=0.9*_np.max(image_avg))

    for it in _trange(iterations, desc='Iterations'):
        # render average image
        N_avg, image_avg = _render.render(locs, **kwargs)
        save(it)
        n_pixel, _ = image_avg.shape
        image_half = n_pixel / 2
        CF_image_avg = _np.conj(_np.fft.fft2(image_avg))
        for i, index in enumerate(_tqdm(group_index, desc='Group alignment', unit='groups')):
            group_locs = locs[index]
            # Storing the original coordinates
            x = group_locs.x.copy()
            y = group_locs.y.copy()
            xcorr_max = 0.0
            for angle in angles:
                # rotate locs
                group_locs.x = _np.cos(angle) * x - _np.sin(angle) * y
                group_locs.y = _np.sin(angle) * x + _np.cos(angle) * y
                # render group image
                N, image = _render.render(group_locs, **kwargs)
                # calculate cross-correlation
                F_image = _np.fft.fft2(image)
                xcorr = _np.fft.fftshift(_np.real(_np.fft.ifft2((F_image * CF_image_avg))))
                # find the brightest pixel
                y_max, x_max = _np.unravel_index(xcorr.argmax(), xcorr.shape)
                # store the transformation if the correlation is larger than before
                if xcorr[y_max, x_max] > xcorr_max:
                    xcorr_max = xcorr[y_max, x_max]
                    rot = angle
                    dy = (y_max - image_half + 0.5) / oversampling
                    dx = (x_max - image_half + 0.5) / oversampling
            # rotate and shift image group locs
            locs.x[index] = _np.cos(rot) * x - _np.sin(rot) * y - dx
            locs.y[index] = _np.sin(rot) * x + _np.cos(rot) * y - dy
    save(iterations)
    return locs
