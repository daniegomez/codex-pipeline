import argparse
import logging
import re
from multiprocessing import Pool
from os import walk
from pathlib import Path
from typing import List, Optional

import lxml.etree
import pandas as pd
import yaml
from aicsimageio import AICSImage
from aicsimageio.vendor.omexml import OMEXML
from aicsimageio.writers import ome_tiff_writer
from tifffile import TiffFile

from utils import print_directory_tree

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s - %(message)s")
logger = logging.getLogger(__name__)


SEGMENTATION_CHANNEL_NAMES = [
    "cells",
    "nuclei",
    "cell_boundaries",
    "nucleus_boundaries",
]


mapping_format_string = """<StructuredAnnotations>
<XMLAnnotation ID="Annotation:0">
    <Value>
        <OriginalMetadata>
            <Key>ProteinIDMap</Key>
            <Value>
                MAP_LINES
            </Value>
        </OriginalMetadata>
    </Value>
</XMLAnnotation>
</StructuredAnnotations>"""


def add_structured_annotations(xml_string, antibodies_df):
    structured_annotation = mapping_format_string.replace(
        "MAP_LINES", create_map_lines(antibodies_df)
    )
    end_index = xml_string.find("</OME>")
    xml_string = xml_string[:end_index] + structured_annotation + xml_string[end_index:]
    return xml_string


def create_map_lines(df):
    map_lines = []
    for i in df.index:
        line = f"""<Channel ID="{df.at[i, 'channel_id']}", ChannelName="{get_analyte_name(df.at[i, 'antibody_name'])}",
         UniprotID = "{df.at[i, 'uniprot_accession_number']}", RRID = "{df.at[i, 'rr_id']}" >"""
        map_lines.append(line)
    return "\n".join(map_lines)


TIFF_FILE_NAMING_PATTERN = re.compile(r"^R\d{3}_X(\d{3})_Y(\d{3})\.tif")
metadata_filename_pattern = re.compile(r"^[0-9A-Fa-f]{32}antibodies\.tsv$")


def generate_sa_ch_info(ch_name: str, antb_info: pd.Series) -> str:
    empty_ch_info = f'<Channel ID="None" Name="{ch_name}" UniprotID="None" RRID="None"/>'
    if antb_info is None:
        ch_info = empty_ch_info
    else:
        ch_id = antb_info.loc["channel_id"]
        uniprot_id = antb_info.loc["uniprot_accession_number"]
        rr_id = antb_info.loc["rr_id"]
        ch_info = f'<Channel ID="{ch_id}" Name="{antb_info.target}" UniprotID="{uniprot_id}" RRID="{rr_id}"/>'
    return ch_info


def generate_structured_annotations(
    original_ch_names: List[str], antb_info: Optional[pd.DataFrame]
) -> str:
    ch_infos = []
    print("Original channel names len vs antibodies df info", len(original_ch_names), antb_info.info())
    print("Original channel names: ", original_ch_names)
    print("antibodies.tsv channel names:", antb_info.target)
    for i, original_ch_name in enumerate(original_ch_names):
        ch_info = generate_sa_ch_info(original_ch_name, antb_info.iloc[i, :])
        ch_infos.append(ch_info)
    ch_sa = "\n".join(ch_infos)
    sa = mapping_format_string.format(protein_id_map_sa=ch_sa)
    return sa


def get_analyte_name(antibody_name):
    return antibody_name.replace("Anti-", "").replace(" antibody", "")


def find_antibodies_meta(input_dir: Path) -> Optional[Path]:
    """
    Finds and returns the first metadata file for a HuBMAP data set.
    Does not check whether the dataset ID (32 hex characters) matches
    the directory name, nor whether there might be multiple metadata files.
    """
    # possible_dirs = [input_dir, input_dir / "extras"]
    metadata_filename_pattern = re.compile(r"^[0-9A-Za-z\-_]*antibodies\.tsv$")
    found_files = []
    for dirpath, dirnames, filenames in walk(input_dir):
        for filename in filenames:
            if metadata_filename_pattern.match(filename):
                found_files.append(Path(dirpath) / filename)

    if len(found_files) == 0:
        logger.warning("No antibody.tsv file found")
        antb_path = None
    else:
        antb_path = found_files[0]
    return antb_path


def sort_by_cycle(antb_path: Path):
    df = pd.read_table(antb_path)
    cycle_channel_pattern = re.compile(r"cycle(?P<cycle>\d+)_ch(?P<channel>\d+)", re.IGNORECASE)
    searches = [cycle_channel_pattern.search(v) for v in df["channel_id"]]
    cycles = [int(s.group("cycle")) for s in searches]
    channels = [int(s.group("channel")) for s in searches]
    df.index = [cycles, channels]
    df = df.sort_index()
    return df


def get_ch_info_from_antibodies_meta(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    # df = df.set_index("channel_id", inplace=False)
    antb_names = df["antibody_name"].to_list()
    antb_targets = [get_analyte_name(antb) for antb in antb_names]
    df["target"] = antb_targets
    return df


# def replace_provider_ch_names_with_antb(provider_ch_names: List[str], antb_ch_info: pd.DataFrame):
#     targets = antb_ch_info["target"].to_list()
#     corrected_ch_names = []
#     for ch in provider_ch_names:
#         new_ch_name = ch
#         for t in targets:
#             if re.match(ch, t, re.IGNORECASE):
#                 new_ch_name = t
#                 break
#         corrected_ch_names.append(new_ch_name)
#     print("Corrected channel names: (from antibodies.tsv) ", corrected_ch_names)
#     return corrected_ch_names


def collect_tiff_file_list(directory: Path, TIFF_FILE_NAMING_PATTERN: re.Pattern) -> List[Path]:
    """
    Given a directory path and a regex, find all the files in the directory that
    match the regex.

    TODO: this is very similar to a function in create_cellshapes_csv.py -- could
    do to unify with a separate module?
    """
    fileList = []

    for dirpath, dirnames, filenames in walk(directory):
        for filename in filenames:
            if TIFF_FILE_NAMING_PATTERN.match(filename):
                fileList.append(directory / filename)

    if len(fileList) == 0:
        logger.warning("No files found in " + str(directory))

    return fileList


def get_lateral_resolution(cytokit_config_filename: Path) -> float:
    with open(cytokit_config_filename) as cytokit_config_file:
        cytokit_config = yaml.safe_load(cytokit_config_file)

    return float("%0.2f" % cytokit_config["acquisition"]["lateral_resolution"])


def collect_expressions_extract_channels(extractFile: Path) -> List[str]:
    """
    Given a TIFF file path, read file with TiffFile to get Labels attribute from
    ImageJ metadata. Return a list of the channel names in the same order as they
    appear in the ImageJ metadata.
    We need to do this to get the channel names in the correct order, and the
    ImageJ "Labels" attribute isn't picked up by AICSImageIO.
    """

    with TiffFile(str(extractFile.absolute())) as TF:
        ij_meta = TF.imagej_metadata
    numChannels = int(ij_meta["channels"])
    channelList = ij_meta["Labels"][0:numChannels]
    print(channelList)

    # Remove "proc_" from the start of the channel names.
    procPattern = re.compile(r"^proc_(.*)")
    channelList = [procPattern.match(channel).group(1) for channel in channelList]
    return channelList


def add_pixel_size_units(omeXml):
    # Don't take any chances about locale environment variables in Docker containers
    # and headless server systems; be explicit about using UTF-8
    encoding = "utf-8"
    omeXmlRoot = lxml.etree.fromstring(omeXml.to_xml(encoding=encoding).encode(encoding))

    namespace_prefix = omeXmlRoot.nsmap[None]
    image_node = omeXmlRoot.find(f"{{{namespace_prefix}}}Image")
    pixels_node = image_node.find(f"{{{namespace_prefix}}}Pixels")

    pixels_node.set("PhysicalSizeXUnit", "nm")
    pixels_node.set("PhysicalSizeYUnit", "nm")

    omexml_with_pixel_units = OMEXML(xml=lxml.etree.tostring(omeXmlRoot))
    return omexml_with_pixel_units


def convert_tiff_file(funcArgs):
    """
    Given a tuple containing a source TIFF file path, a destination OME-TIFF path,
    a list of channel names, a float value for the lateral resolution in
    nanometres, convert the source TIFF file to OME-TIFF format, containing
    polygons for segmented cell shapes in the "ROI" OME-XML element.
    """
    sourceFile, ometiffFile, channelNames, lateral_resolution, struct_annot = funcArgs

    logger.info(f"Converting file: { str( sourceFile ) }")

    image = AICSImage(sourceFile)

    imageDataForOmeTiff = image.get_image_data("TCZYX")

    # Create a template OME-XML object.
    omeXml = OMEXML()

    # Populate it with image metadata.
    omeXml.image().Pixels.set_SizeT(image.size_t)
    omeXml.image().Pixels.set_SizeC(image.size_c)
    omeXml.image().Pixels.set_SizeZ(image.size_z)
    omeXml.image().Pixels.set_SizeY(image.size_y)
    omeXml.image().Pixels.set_SizeX(image.size_x)
    omeXml.image().Pixels.set_PixelType(str(imageDataForOmeTiff.dtype))
    omeXml.image().Pixels.set_DimensionOrder("XYZCT")
    omeXml.image().Pixels.channel_count = len(channelNames)
    omeXml.image().Pixels.set_PhysicalSizeX(lateral_resolution)
    omeXml.image().Pixels.set_PhysicalSizeY(lateral_resolution)

    omeXml = add_pixel_size_units(omeXml)
    # print("Channel names (should match antibodies.tsv):", channelNames)
    for i in range(0, len(channelNames)):
        omeXml.image().Pixels.Channel(i).Name = channelNames[i]
        omeXml.image().Pixels.Channel(i).ID = "Channel:0:" + str(i)
    # Convert and manipulate OME metadata as string here
    omeXml = add_structured_annotations(omeXml.to_xml("utf-8"))

    with ome_tiff_writer.OmeTiffWriter(ometiffFile) as ome_writer:
        ome_writer.save(
            imageDataForOmeTiff,
            ome_xml=omeXml,
            dimension_order="TCZYX",
            channel_names=channelNames,
        )
        print(ome_writer.size_c())

    logger.info(f"OME-TIFF file created: { ometiffFile }")


def create_ome_tiffs(
    file_list: List[Path],
    output_dir: Path,
    channel_names: List[str],
    lateral_resolution: float,
    subprocesses: int,
    struct_annot: Optional[str] = None,
):
    """
    Given:
        - a list of TIFF files
        - an output directory path
        - a list of channel names
        - a float value for the lateral resolution in nanometres (aka XY resolution aka pixel size).
        - an integer value for the number of multiprocessing subprocesses
        - a dictionary of best focus z-planes indexed by tile x,y coordinates
    Create OME-TIFF files using parallel processes.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    args_for_conversion = []

    for source_file in file_list:
        ome_tiff_file = (output_dir / source_file.name).with_suffix(".ome.tiff")

        args_for_conversion.append(
            (source_file, ome_tiff_file, channel_names, lateral_resolution, struct_annot)
        )

    # for argtuple in args_for_conversion :
    #    convert_tiff_file( argtuple )

    with Pool(processes=subprocesses) as pool:
        pool.imap_unordered(convert_tiff_file, args_for_conversion)
        pool.close()
        pool.join()


def check_dir_is_empty(dir_path: Path):
    return not any(dir_path.iterdir())


########
# MAIN #
########
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Convert Cytokit's output TIFFs containing segmentation and extraction "
            'results to OME-TIFF, and add the channel names. Creates an "ome-tiff" '
            "directory inside the output/cytometry/tile and "
            "output/extract/expressions directories."
        ),
    )
    parser.add_argument(
        "cytokit_output",
        help="Path to output of `cytokit processor`",
        type=Path,
    )
    parser.add_argument(
        "bg_sub_tiles",
        help="Path to tiles with subtracted background",
        type=Path,
    )
    parser.add_argument(
        "cytokit_config",
        help="Path to Cytokit YAML config file",
        type=Path,
    )
    parser.add_argument(
        "input_data_dir",
        help="Path to the input dataset",
        type=Path,
    )
    parser.add_argument(
        "-p",
        "--processes",
        help="Number of parallel OME-TIFF conversions to perform at once",
        type=int,
        default=8,
    )

    args = parser.parse_args()

    print("Cytokit output:")
    print_directory_tree(args.cytokit_output)

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    cytometry_tile_dir_piece = Path("cytometry/tile")
    extract_expressions_piece = Path("extract/expressions")
    processor_data_json_piece = Path("processor/data.json")

    cytometryTileDir = args.cytokit_output / cytometry_tile_dir_piece
    print("Cytometry tile directory:", cytometryTileDir)

    extractDir = args.cytokit_output / extract_expressions_piece
    print("Extract expressions directory:", extractDir)

    if not check_dir_is_empty(args.bg_sub_tiles):
        extractDir = args.bg_sub_tiles
        print(list(Path(args.bg_sub_tiles).iterdir()))
    else:
        extractDir = args.cytokit_output / extract_expressions_piece
    print("Extract expressions directory:", extractDir)

    segmentationFileList = collect_tiff_file_list(cytometryTileDir, TIFF_FILE_NAMING_PATTERN)
    extractFileList = collect_tiff_file_list(extractDir, TIFF_FILE_NAMING_PATTERN)
    antb_path = find_antibodies_meta(args.input_data_dir)

    lateral_resolution = get_lateral_resolution(args.cytokit_config)

    # Create segmentation mask OME-TIFFs
    if segmentationFileList:
        create_ome_tiffs(
            segmentationFileList,
            output_dir / cytometry_tile_dir_piece / "ome-tiff",
            SEGMENTATION_CHANNEL_NAMES,
            lateral_resolution,
            args.processes,
        )

    # Create the extract OME-TIFFs.
    if extractFileList:
        # For the extract, pull the correctly ordered list of channel names from
        # one of the files, as they aren't guaranteed to be in the same order as
        # the YAML config.
        df = sort_by_cycle(antb_path)
        antb_info = get_ch_info_from_antibodies_meta(df)
        original_ch_names = collect_expressions_extract_channels(extractFileList[0])
        struct_annot = generate_structured_annotations(original_ch_names, antb_info)
        print(struct_annot)
        create_ome_tiffs(
            extractFileList,
            output_dir / extract_expressions_piece / "ome-tiff",
            list(antb_info.target),
            lateral_resolution,
            args.processes,
            struct_annot,
        )
