from pathlib import Path
from typing import List

import numpy as np
import tifffile as tif
import cv2 as cv

Image = np.ndarray


def _laplacian_variance(img: Image) -> float:
    """
    DOI:10.1016/j.patcog.2012.11.011
    Analysis of focus measure operators for shape-from-focus
    """
    return np.var(cv.Laplacian(img, cv.CV_64F, ksize=21))


def _find_best_z_plane_id(img_list: List[Image]) -> int:
    lap_vars_per_z_plane = []
    for img in img_list:
        lap_vars_per_z_plane.append(_laplacian_variance(img))
    max_var = max(lap_vars_per_z_plane)
    max_var_id = lap_vars_per_z_plane.index(max_var)
    return max_var_id


def _load_images(path_list: List[Path]) -> List[Image]:
    img_list = []
    for path in path_list:
        img_list.append(tif.imread(str(path)))
    return img_list


def get_best_z_plane_id(path_list: List[Path]) -> int:
    img_list = _load_images(path_list)
    return _find_best_z_plane_id(img_list) + 1


def median_error_cor(array: np.array, mode: str) -> np.array:
    """ Replace all values in rows or cols with respective medians"""
    arr = array.copy()
    if mode == 'row':
        nrows = arr.shape[0]
        row_medians = np.nanmedian(arr, axis=1)
        total_row_median = np.nanmedian(row_medians)  # median of medians
        row_medians = list(np.nan_to_num(row_medians, nan=total_row_median))  # replace NaNs with median of medians
        for i in range(0, nrows):
            arr[i, :] = int(round(row_medians[i]))
    elif mode == 'col':
        ncols = arr.shape[1]
        col_medians = np.nanmedian(arr, axis=0)
        total_col_median = np.nanmedian(col_medians)
        col_medians = list(np.nan_to_num(col_medians, nan=total_col_median))
        for i in range(0, ncols):
            arr[:, i] = int(round(col_medians[i]))

    return arr


def change_tile_location_according_to_tiling_mode(array: np.ndarray, tiling_mode: str):
    if tiling_mode == 'grid':
        pass
    elif tiling_mode == 'snake':
        nrows = array.shape[0]
        for i in range(0, nrows):
            if i % 2 != 0:
                array[i, :] = array[i, :][::-1]  # reverse this row

    return array


def validate_best_z(best_z_plane_id_list: List[int], x_ntiles: int, y_ntiles: int, tiling_mode: str):
    best_z_per_tile_array = np.array(best_z_plane_id_list, dtype=np.int32).reshape(y_ntiles, x_ntiles)

    rearranged_best_z_per_tile_array = change_tile_location_according_to_tiling_mode(best_z_per_tile_array, tiling_mode)
    corrected_best_z_per_tile_array = median_error_cor(rearranged_best_z_per_tile_array, 'col')
    restored_arrangement_best_z_per_tile_array = change_tile_location_according_to_tiling_mode(corrected_best_z_per_tile_array, tiling_mode)

    result = restored_arrangement_best_z_per_tile_array.ravel().tolist()

    return result


def pick_z_planes_below_and_above(best_z: int, max_z: int, above: int, below: int) -> List[int]:
    above_check = best_z + above - max_z
    if above_check > 0:
        above -= above_check

    below_check = best_z - below
    if below_check < 0:
        below += below_check

    if max_z == 1:
        return [best_z]
    elif best_z == max_z:
        below_planes = [best_z - i for i in range(1, below + 1)]
        above_planes = []
    elif best_z == 1:
        below_planes = []
        above_planes = [best_z + i for i in range(1, above + 1)]
    else:
        below_planes = [best_z - i for i in range(1, below + 1)]
        above_planes = [best_z + i for i in range(1, above + 1)]

    return below_planes + [best_z] + above_planes


def get_best_z_plane_ids_per_tile(plane_paths_per_tile: dict, x_ntiles: int, y_ntiles: int, max_z: int, tiling_mode: str):
    best_z_plane_id_list = []
    for tile, plane_paths in plane_paths_per_tile.items():
        plane_path_list = list(plane_paths.values())
        best_z_plane_id = get_best_z_plane_id(plane_path_list)
        best_z_plane_id_list.append(best_z_plane_id)
    valid_best_z_plane_id_list = validate_best_z(best_z_plane_id_list, x_ntiles, y_ntiles, tiling_mode)

    best_z_plane_per_tile = dict()
    for i, tile in enumerate(plane_paths_per_tile.keys()):
        best_z_plane_per_tile[tile] = pick_z_planes_below_and_above(valid_best_z_plane_id_list[i], max_z, 1, 1)
    return best_z_plane_per_tile