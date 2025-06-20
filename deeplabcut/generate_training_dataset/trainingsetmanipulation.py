#
# DeepLabCut Toolbox (deeplabcut.org)
# © A. & M.W. Mathis Labs
# https://github.com/DeepLabCut/DeepLabCut
#
# Please see AUTHORS for contributors.
# https://github.com/DeepLabCut/DeepLabCut/blob/master/AUTHORS
#
# Licensed under GNU Lesser General Public License v3.0
#
from __future__ import annotations

import math
import logging
import os
import os.path
import warnings

from functools import lru_cache
from pathlib import Path
from PIL import Image
from typing import List

import numpy as np
import pandas as pd
import yaml

import deeplabcut.compat as compat
import deeplabcut.generate_training_dataset.metadata as metadata
from deeplabcut.core.engine import Engine
from deeplabcut.core.weight_init import WeightInitialization
from deeplabcut.utils import (
    auxiliaryfunctions,
    conversioncode,
    auxfun_models,
    auxfun_multianimal,
)
from deeplabcut.utils.auxfun_videos import VideoReader


def comparevideolistsanddatafolders(config):
    """
    Auxiliary function that compares the folders in labeled-data and the ones listed under video_sets (in the config file).

    Parameter
    ----------
    config : string
        String containing the full path of the config file in the project.

    """
    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"].keys()
    video_names = [Path(i).stem for i in videos]
    alldatafolders = [
        fn
        for fn in os.listdir(Path(config).parent / "labeled-data")
        if "_labeled" not in fn
    ]

    print("Config file contains:", len(video_names))
    print("Labeled-data contains:", len(alldatafolders))

    for vn in video_names:
        if vn not in alldatafolders:
            print(vn, " is missing as a folder!")

    for vn in alldatafolders:
        if vn not in video_names:
            print(vn, " is missing in config file!")


def adddatasetstovideolistandviceversa(config):
    """
    First run comparevideolistsanddatafolders(config) to compare the folders in labeled-data and the ones listed under video_sets (in the config file).
    If you detect differences this function can be used to maker sure each folder has a video entry & vice versa.

    It corrects this problem in the following way:

    If a video entry in the config file does not contain a folder in labeled-data, then the entry is removed.
    If a folder in labeled-data does not contain a video entry in the config file then the prefix path will be added in front of the name of the labeled-data folder and combined
    with the suffix variable as an ending. Width and height will be added as cropping variables as passed on.

    Handle with care!

    Parameter
    ----------
    config : string
        String containing the full path of the config file in the project.
    """
    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"]
    video_names = [Path(i).stem for i in videos]

    alldatafolders = [
        fn
        for fn in os.listdir(Path(config).parent / "labeled-data")
        if "_labeled" not in fn and not fn.startswith(".")
    ]

    print("Config file contains:", len(video_names))
    print("Labeled-data contains:", len(alldatafolders))

    toberemoved = []
    for vn in video_names:
        if vn not in alldatafolders:
            print(vn, " is missing as a labeled folder >> removing key!")
            for fullvideo in videos:
                if vn in fullvideo:
                    toberemoved.append(fullvideo)

    for vid in toberemoved:
        del videos[vid]

    # Load updated lists:
    video_names = [Path(i).stem for i in videos]
    for vn in alldatafolders:
        if vn not in video_names:
            print(vn, " is missing in config file >> adding it!")
            # Find the corresponding video file
            found = False
            for file in os.listdir(os.path.join(cfg["project_path"], "videos")):
                if os.path.splitext(file)[0] == vn:
                    found = True
                    break
            if found:
                video_path = os.path.join(cfg["project_path"], "videos", file)
                clip = VideoReader(video_path)
                videos.update(
                    {video_path: {"crop": ", ".join(map(str, clip.get_bbox()))}}
                )

    auxiliaryfunctions.write_config(config, cfg)


def dropduplicatesinannotatinfiles(config):
    """

    Drop duplicate entries (of images) in annotation files (this should no longer happen, but might be useful).

    Parameter
    ----------
    config : string
        String containing the full path of the config file in the project.

    """
    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"].keys()
    video_names = [Path(i).stem for i in videos]
    folders = [Path(config).parent / "labeled-data" / Path(i) for i in video_names]

    for folder in folders:
        try:
            fn = os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".h5")
            DC = pd.read_hdf(fn)
            numimages = len(DC.index)
            DC = DC[~DC.index.duplicated(keep="first")]
            if len(DC.index) < numimages:
                print("Dropped", numimages - len(DC.index))
                DC.to_hdf(fn, key="df_with_missing", mode="w")
                DC.to_csv(
                    os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".csv")
                )

        except FileNotFoundError:
            print("Attention:", folder, "does not appear to have labeled data!")


def dropannotationfileentriesduetodeletedimages(config):
    """
    Drop entries for all deleted images in annotation files, i.e. for folders of the type: /labeled-data/*folder*/CollectedData_*scorer*.h5
    Will be carried out iteratively for all *folders* in labeled-data.

    Parameter
    ----------
    config : string
        String containing the full path of the config file in the project.

    """
    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"].keys()
    video_names = [Path(i).stem for i in videos]
    folders = [Path(config).parent / "labeled-data" / Path(i) for i in video_names]

    for folder in folders:
        fn = os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".h5")
        try:
            DC = pd.read_hdf(fn)
        except FileNotFoundError:
            print("Attention:", folder, "does not appear to have labeled data!")
            continue
        dropped = False
        for imagename in DC.index:
            if os.path.isfile(os.path.join(cfg["project_path"], *imagename)):
                pass
            else:
                print("Dropping...", imagename)
                DC = DC.drop(imagename)
                dropped = True
        if dropped == True:
            DC.to_hdf(fn, key="df_with_missing", mode="w")
            DC.to_csv(
                os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".csv")
            )


def dropimagesduetolackofannotation(config):
    """
    Drop images from corresponding folder for not annotated images: /labeled-data/*folder*/CollectedData_*scorer*.h5
    Will be carried out iteratively for all *folders* in labeled-data.

    Parameter
    ----------
    config : string
        String containing the full path of the config file in the project.
    """
    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"].keys()
    video_names = [Path(i).stem for i in videos]
    folders = [Path(config).parent / "labeled-data" / Path(i) for i in video_names]

    for folder in folders:
        h5file = os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".h5")
        try:
            DC = pd.read_hdf(h5file)
        except FileNotFoundError:
            print("Attention:", folder, "does not appear to have labeled data!")
            continue
        conversioncode.guarantee_multiindex_rows(DC)
        annotatedimages = [fn[-1] for fn in DC.index]
        imagelist = [fns for fns in os.listdir(str(folder)) if ".png" in fns]
        print("Annotated images: ", len(annotatedimages), " In folder:", len(imagelist))
        for imagename in imagelist:
            if imagename in annotatedimages:
                pass
            else:
                fullpath = os.path.join(
                    cfg["project_path"], "labeled-data", folder, imagename
                )
                if os.path.isfile(fullpath):
                    print("Deleting", fullpath)
                    os.remove(fullpath)

        annotatedimages = [fn[-1] for fn in DC.index]
        imagelist = [fns for fns in os.listdir(str(folder)) if ".png" in fns]
        print(
            "PROCESSED:",
            folder,
            " now # of annotated images: ",
            len(annotatedimages),
            " in folder:",
            len(imagelist),
        )


def dropunlabeledframes(config):
    """
    Drop entries such that all the bodyparts are not labeled from the annotation files, i.e. h5 and csv files
    Will be carried out iteratively for all *folders* in labeled-data.

    Parameter
    ----------
    config : string
        String containing the full path of the config file in the project.

    """
    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"].keys()
    video_names = [Path(i).stem for i in videos]
    folders = [Path(config).parent / "labeled-data" / Path(i) for i in video_names]

    for folder in folders:
        h5file = os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".h5")
        try:
            DC = pd.read_hdf(h5file)
        except FileNotFoundError:
            print("Skipping ", folder, "...")
            continue
        before_len = len(DC.index)
        DC = DC.dropna(how="all")  # drop rows where all values are missing(NaN)
        after_len = len(DC.index)
        dropped = before_len - after_len
        if dropped:
            DC.to_hdf(h5file, key="df_with_missing", mode="w")
            DC.to_csv(
                os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".csv")
            )

            print("Dropped ", dropped, "entries in ", folder)

    print("Done.")


def check_labels(
    config,
    Labels=["+", ".", "x"],
    scale=1,
    dpi=100,
    draw_skeleton=True,
    visualizeindividuals=True,
):
    """Check the labeled frames.

    Double check if the labels were at the correct locations and stored in the proper
    file format.

    This creates a new subdirectory for each video under the 'labeled-data' and all the
    frames are plotted with the labels.

    Make sure that these labels are fine.

    Parameters
    ----------
    config : string
        Full path of the config.yaml file as a string.

    Labels: list, default='+'
        List of at least 3 matplotlib markers. The first one will be used to indicate
        the human ground truth location (Default: +)

    scale : float, default=1
        Change the relative size of the output images.

    dpi : int, optional, default=100
        Output resolution in dpi.

    draw_skeleton: bool, default=True
        Plot skeleton overlaid over body parts.

    visualizeindividuals: bool, default: True.
        For a multianimal project, if True, the different individuals have different
        colors (and all bodyparts the same). If False, the colors change over bodyparts
        rather than individuals.

    Returns
    -------
    None

    Examples
    --------
    >>> deeplabcut.check_labels('/analysis/project/reaching-task/config.yaml')
    """

    from deeplabcut.utils import visualization

    cfg = auxiliaryfunctions.read_config(config)
    videos = cfg["video_sets"].keys()
    video_names = [_robust_path_split(video)[1] for video in videos]

    folders = [
        os.path.join(cfg["project_path"], "labeled-data", str(Path(i)))
        for i in video_names
    ]
    print("Creating images with labels by %s." % cfg["scorer"])
    for folder in folders:
        try:
            DataCombined = pd.read_hdf(
                os.path.join(str(folder), "CollectedData_" + cfg["scorer"] + ".h5")
            )
            conversioncode.guarantee_multiindex_rows(DataCombined)
            if cfg.get("multianimalproject", False):
                color_by = "individual" if visualizeindividuals else "bodypart"
            else:  # for single animal projects
                color_by = "bodypart"

            visualization.make_labeled_images_from_dataframe(
                DataCombined,
                cfg,
                folder,
                scale,
                dpi=dpi,
                keypoint=Labels[0],
                draw_skeleton=draw_skeleton,
                color_by=color_by,
            )
        except FileNotFoundError:
            print("Attention:", folder, "does not appear to have labeled data!")

    print(
        "If all the labels are ok, then use the function 'create_training_dataset' to create the training dataset!"
    )


def boxitintoacell(joints):
    """Auxiliary function for creating matfile."""
    outer = np.array([[None]], dtype=object)
    outer[0, 0] = np.array(joints, dtype="int64")
    return outer


def ParseYaml(configfile):
    raw = open(configfile).read()
    docs = []
    for raw_doc in raw.split("\n---"):
        try:
            docs.append(yaml.load(raw_doc, Loader=yaml.SafeLoader))
        except SyntaxError:
            docs.append(raw_doc)
    return docs


def MakeTrain_pose_yaml(
    itemstochange,
    saveasconfigfile,
    defaultconfigfile,
    items2drop: dict | None = None,
    save: bool = True,
):
    if items2drop is None:
        items2drop = {}

    docs = ParseYaml(defaultconfigfile)
    for key in items2drop.keys():
        if key in docs[0].keys():
            docs[0].pop(key)

    for key in itemstochange.keys():
        docs[0][key] = itemstochange[key]

    if save:
        with open(saveasconfigfile, "w") as f:
            yaml.dump(docs[0], f)

    return docs[0]


def MakeTest_pose_yaml(
    dictionary,
    keys2save,
    saveasfile,
    nmsradius=None,
    minconfidence=None,
    sigma=None,
    locref_smooth=None,
):
    dict_test = {}
    for key in keys2save:
        dict_test[key] = dictionary[key]

    # adding important values for multianiaml project:
    if nmsradius is not None:
        dict_test["nmsradius"] = nmsradius
    if minconfidence is not None:
        dict_test["minconfidence"] = minconfidence
    if sigma is not None:
        dict_test["sigma"] = sigma
    if locref_smooth is not None:
        dict_test["locref_smooth"] = locref_smooth

    dict_test["scoremap_dir"] = "test"
    with open(saveasfile, "w") as f:
        yaml.dump(dict_test, f)


def MakeInference_yaml(itemstochange, saveasconfigfile, defaultconfigfile):
    docs = ParseYaml(defaultconfigfile)
    for key in itemstochange.keys():
        docs[0][key] = itemstochange[key]

    with open(saveasconfigfile, "w") as f:
        yaml.dump(docs[0], f)
    return docs[0]


def _robust_path_split(path):
    sep = "\\" if "\\" in path else "/"
    splits = path.rsplit(sep, 1)
    if len(splits) == 1:
        parent = "."
        file = splits[0]
    elif len(splits) == 2:
        parent, file = splits
    else:
        raise ("Unknown filepath split for path {}".format(path))
    filename, ext = os.path.splitext(file)
    return parent, filename, ext


def parse_video_filenames(videos: List[str]) -> List[str]:
    """Parses the names of all videos listed in a project's ``config.yaml`` file

    Goes through the paths all videos listed for a project, and removes entries with a
    duplicate video name (e.g. if a video is listed twice, once with the path
    ``/data/video-1.mov`` and once with the path ``/my-dlc-project/videos/video-1.mov``,
    then ``video-1`` will only be returned once). The order of videos listed is
    preserved.

    This prevents the same labeled-data to be added multiple times when merging
    annotated datasets.

    Prints a warning for each filename with duplicate video paths.

    Args:
        videos: the videos listed in the project's config.yaml file

    Returns:
        the filenames of videos listed in the project's config.yaml file, with duplicate
        entries removed
    """
    filenames = []
    filename_to_videos = {}
    for video in videos:
        if Path(video).suffix.lower() == ".avi":
            _, filename, _ = _robust_path_split(video)
        else:
            filename = Path(video).stem  # Get the filename without extension
            parent = Path(video).parent.name
            parent_parent = Path(video).parent.parent.name
            filename = f'{parent_parent}_{parent}_{filename}'
            print(f'Using filename: {filename} for parent: {parent} and parent_parent: {parent_parent}')
        videos_with_filename = filename_to_videos.get(filename, [])
        if len(videos_with_filename) == 0:
            filenames.append(filename)

        videos_with_filename.append(video)
        filename_to_videos[filename] = videos_with_filename

    for filename, videos in filename_to_videos.items():
        if len(videos) > 1:
            video_str = "\n  * " + "\n  * ".join(videos)
            logging.warning(
                f"Found multiple videos with the same filename (``{filename}``). To "
                f"avoid issues, please edit your project's `config.yaml` file to have "
                f"each video added only once.\nDuplicate entries: {video_str}"
            )

    return filenames


def merge_annotateddatasets(cfg, trainingsetfolder_full):
    """
    Merges all the h5 files for all labeled-datasets (from individual videos).

    This is a bit of a mess because of cross platform compatibility.

    Within platform comp. is straightforward. But if someone labels on windows and wants to train on a unix cluster or colab...
    """
    AnnotationData = []
    data_path = Path(os.path.join(cfg["project_path"], "labeled-data"))
    videos = cfg["video_sets"].keys()
    video_filenames = parse_video_filenames(videos)
    for filename in video_filenames:
        file_path = os.path.join(
            data_path / filename, f'CollectedData_{cfg["scorer"]}.h5'
        )
        try:
            data = pd.read_hdf(file_path)
            conversioncode.guarantee_multiindex_rows(data)
            if data.columns.levels[0][0] != cfg["scorer"]:
                print(
                    f"{file_path} labeled by a different scorer. This data will not be utilized in training dataset creation. If you need to merge datasets across scorers, see https://github.com/DeepLabCut/DeepLabCut/wiki/Using-labeled-data-in-DeepLabCut-that-was-annotated-elsewhere-(or-merge-across-labelers)"
                )
                continue
            AnnotationData.append(data)
        except FileNotFoundError:
            print(file_path, " not found (perhaps not annotated).")

    if not len(AnnotationData):
        print(
            "Annotation data was not found by splitting video paths (from config['video_sets']). An alternative route is taken..."
        )
        AnnotationData = conversioncode.merge_windowsannotationdataONlinuxsystem(cfg)
        if not len(AnnotationData):
            print("No data was found!")
            return

    AnnotationData = pd.concat(AnnotationData).sort_index()
    # When concatenating DataFrames with misaligned column labels,
    # all sorts of reordering may happen (mainly depending on 'sort' and 'join')
    # Ensure the 'bodyparts' level agrees with the order in the config file.
    if cfg.get("multianimalproject", False):
        (
            _,
            uniquebodyparts,
            multianimalbodyparts,
        ) = auxfun_multianimal.extractindividualsandbodyparts(cfg)
        bodyparts = multianimalbodyparts + uniquebodyparts
    else:
        bodyparts = cfg["bodyparts"]
    AnnotationData = AnnotationData.reindex(
        bodyparts, axis=1, level=AnnotationData.columns.names.index("bodyparts")
    )
    filename = os.path.join(trainingsetfolder_full, f'CollectedData_{cfg["scorer"]}')
    AnnotationData.to_hdf(filename + ".h5", key="df_with_missing", mode="w")
    AnnotationData.to_csv(filename + ".csv")  # human readable.
    return AnnotationData


def SplitTrials(
    trialindex,
    trainFraction=0.8,
    enforce_train_fraction=False,
):
    """Split a trial index into train and test sets. Also checks that the trainFraction is a two digit number between 0 an 1. The reason
    is that the folders contain the trainfraction as int(100*trainFraction).
    If enforce_train_fraction is True, train and test indices are padded with -1
    such that the ratio of their lengths is exactly the desired train fraction.
    """
    if trainFraction > 1 or trainFraction < 0:
        print(
            "The training fraction should be a two digit number between 0 and 1; i.e. 0.95. Please change accordingly."
        )
        return ([], [])

    if abs(trainFraction - round(trainFraction, 2)) > 0:
        print(
            "The training fraction should be a two digit number between 0 and 1; i.e. 0.95. Please change accordingly."
        )
        return ([], [])
    else:
        index_len = len(trialindex)
        train_fraction = round(trainFraction, 2)
        train_size = index_len * train_fraction
        shuffle = np.random.permutation(trialindex)
        test_indices = shuffle[int(train_size) :]
        train_indices = shuffle[: int(train_size)]
        if enforce_train_fraction and not train_size.is_integer():
            train_indices, test_indices = pad_train_test_indices(
                train_indices,
                test_indices,
                train_fraction,
            )

        return train_indices, test_indices


def pad_train_test_indices(train_inds, test_inds, train_fraction):
    n_train_inds = len(train_inds)
    n_test_inds = len(test_inds)
    index_len = n_train_inds + n_test_inds
    if n_train_inds / index_len == train_fraction:
        return

    # Determine the index length required to guarantee
    # the train–test ratio is exactly the desired one.
    min_length_req = int(100 / math.gcd(100, int(round(100 * train_fraction))))
    min_n_train = int(round(min_length_req * train_fraction))
    min_n_test = min_length_req - min_n_train
    mult = max(
        math.ceil(n_train_inds / min_n_train),
        math.ceil(n_test_inds / min_n_test),
    )
    n_train = mult * min_n_train
    n_test = mult * min_n_test
    # Pad indices so lengths agree
    train_inds = np.append(train_inds, [-1] * (n_train - n_train_inds))
    test_inds = np.append(test_inds, [-1] * (n_test - n_test_inds))
    return train_inds, test_inds


def mergeandsplit(config, trainindex=0, uniform=True):
    """
    This function allows additional control over "create_training_dataset".

    Merge annotated data sets (from different folders) and split data in a specific way, returns the split variables (train/test indices).
    Importantly, this allows one to freeze a split.

    One can also either create a uniform split (uniform = True; thereby indexing TrainingFraction in config file) or leave-one-folder out split
    by passing the index of the corresponding video from the config.yaml file as variable trainindex.

    Parameter
    ----------
    config : string
        Full path of the config.yaml file as a string.

    trainindex: int, optional
        Either (in case uniform = True) indexes which element of TrainingFraction in the config file should be used (note it is a list!).
        Alternatively (uniform = False) indexes which folder is dropped, i.e. the first if trainindex=0, the second if trainindex =1, etc.

    uniform: bool, optional
        Perform uniform split (disregarding folder structure in labeled data), or (if False) leave one folder out.

    Examples
    --------
    To create a leave-one-folder-out model:
    >>> trainIndices, testIndices=deeplabcut.mergeandsplit(config,trainindex=0,uniform=False)
    returns the indices for the first video folder (as defined in config file) as testIndices and all others as trainIndices.
    You can then create the training set by calling (e.g. defining it as Shuffle 3):
    >>> deeplabcut.create_training_dataset(config,Shuffles=[3],trainIndices=trainIndices,testIndices=testIndices)

    To freeze a (uniform) split (i.e. iid sampled from all the data):
    >>> trainIndices, testIndices=deeplabcut.mergeandsplit(config,trainindex=0,uniform=True)

    You can then create two model instances that have the identical trainingset. Thereby you can assess the role of various parameters on the performance of DLC.
    >>> deeplabcut.create_training_dataset(config,Shuffles=[0,1],trainIndices=[trainIndices, trainIndices],testIndices=[testIndices, testIndices])
    --------

    """
    # Loading metadata from config file:
    cfg = auxiliaryfunctions.read_config(config)
    scorer = cfg["scorer"]
    project_path = cfg["project_path"]
    # Create path for training sets & store data there
    trainingsetfolder = auxiliaryfunctions.get_training_set_folder(
        cfg
    )  # Path concatenation OS platform independent
    auxiliaryfunctions.attempt_to_make_folder(
        Path(os.path.join(project_path, str(trainingsetfolder))), recursive=True
    )
    fn = os.path.join(project_path, trainingsetfolder, "CollectedData_" + cfg["scorer"])

    try:
        Data = pd.read_hdf(fn + ".h5")
    except FileNotFoundError:
        Data = merge_annotateddatasets(
            cfg,
            Path(os.path.join(project_path, trainingsetfolder)),
        )
        if Data is None:
            return [], []

    conversioncode.guarantee_multiindex_rows(Data)
    Data = Data[scorer]  # extract labeled data

    if uniform == True:
        TrainingFraction = cfg["TrainingFraction"]
        trainFraction = TrainingFraction[trainindex]
        trainIndices, testIndices = SplitTrials(
            range(len(Data.index)),
            trainFraction,
            True,
        )
    else:  # leave one folder out split
        videos = cfg["video_sets"].keys()
        test_video_name = [Path(i).stem for i in videos][trainindex]
        print("Excluding the following folder (from training):", test_video_name)
        trainIndices, testIndices = [], []
        for index, name in enumerate(Data.index):
            if test_video_name == name[1]:  # this is the video name
                # print(name,test_video_name)
                testIndices.append(index)
            else:
                trainIndices.append(index)

    return trainIndices, testIndices


@lru_cache(maxsize=None)
def read_image_shape_fast(path):
    # Blazing fast and does not load the image into memory
    with Image.open(path) as img:
        width, height = img.size
        return len(img.getbands()), height, width


def format_training_data(df, train_inds, nbodyparts, project_path):
    train_data = []
    matlab_data = []

    def to_matlab_cell(array):
        outer = np.array([[None]], dtype=object)
        outer[0, 0] = array.astype("int64")
        return outer

    for i in train_inds:
        data = dict()
        filename = df.index[i]
        data["image"] = filename
        img_shape = read_image_shape_fast(os.path.join(project_path, *filename))
        data["size"] = img_shape
        temp = df.iloc[i].values.reshape(-1, 2)
        joints = np.c_[range(nbodyparts), temp]
        joints = joints[~np.isnan(joints).any(axis=1)].astype(int)
        # Check that points lie within the image
        inside = np.logical_and(
            np.logical_and(joints[:, 1] < img_shape[2], joints[:, 1] > 0),
            np.logical_and(joints[:, 2] < img_shape[1], joints[:, 2] > 0),
        )
        if not all(inside):
            joints = joints[inside]
        if joints.size:  # Exclude images without labels
            data["joints"] = joints
            train_data.append(data)
            matlab_data.append(
                (
                    np.array([data["image"]], dtype="U"),
                    np.array([data["size"]]),
                    to_matlab_cell(data["joints"]),
                )
            )
    matlab_data = np.asarray(
        matlab_data, dtype=[("image", "O"), ("size", "O"), ("joints", "O")]
    )
    return train_data, matlab_data


def create_training_dataset(
    config,
    num_shuffles=1,
    Shuffles=None,
    windows2linux=False,
    userfeedback=True,
    trainIndices=None,
    testIndices=None,
    net_type=None,
    detector_type=None,
    augmenter_type=None,
    posecfg_template=None,
    superanimal_name="",
    weight_init: WeightInitialization | None = None,
    engine: Engine | None = None,
    ctd_conditions: int | str | Path | tuple[int, str] | tuple[int, int] | None = None,
):
    """Creates a training dataset.

    Labels from all the extracted frames are merged into a single .h5 file.
    Only the videos included in the config file are used to create this dataset.

    Parameters
    ----------
    config : string
        Full path of the ``config.yaml`` file as a string.

    num_shuffles : int, optional, default=1
        Number of shuffles of training dataset to create, i.e. ``[1,2,3]`` for
        ``num_shuffles=3``.

    Shuffles: list[int], optional
        Alternatively the user can also give a list of shuffles.

    userfeedback: bool, optional, default=True
        If ``False``, all requested train/test splits are created (no matter if they
        already exist). If you want to assure that previous splits etc. are not
        overwritten, set this to ``True`` and you will be asked for each split.

    trainIndices: list of lists, optional, default=None
        List of one or multiple lists containing train indexes.
        A list containing two lists of training indexes will produce two splits.

    testIndices: list of lists, optional, default=None
        List of one or multiple lists containing test indexes.

    net_type: list, optional, default=None
        Type of networks. The options available depend on which engine is used.
        Currently supported options are:
            TensorFlow
                * ``resnet_50``
                * ``resnet_101``
                * ``resnet_152``
                * ``mobilenet_v2_1.0``
                * ``mobilenet_v2_0.75``
                * ``mobilenet_v2_0.5``
                * ``mobilenet_v2_0.35``
                * ``efficientnet-b0``
                * ``efficientnet-b1``
                * ``efficientnet-b2``
                * ``efficientnet-b3``
                * ``efficientnet-b4``
                * ``efficientnet-b5``
                * ``efficientnet-b6``
            PyTorch (call ``deeplabcut.pose_estimation_pytorch.available_models()`` for
            a complete list)
                * ``animaltokenpose_base``
                * ``cspnext_m``
                * ``cspnext_s``
                * ``cspnext_x``
                * ``ctd_coam_w32``
                * ``ctd_coam_w48``
                * ``ctd_prenet_cspnext_m``
                * ``ctd_prenet_cspnext_x``
                * ``ctd_prenet_rtmpose_x_human``
                * ``ctd_prenet_hrnet_w32``
                * ``ctd_prenet_hrnet_w48``
                * ``ctd_prenet_rtmpose_m``
                * ``ctd_prenet_rtmpose_x``
                * ``ctd_prenet_rtmpose_x_human``
                * ``dekr_w18``
                * ``dekr_w32``
                * ``dekr_w48``
                * ``dlcrnet_stride16_ms5``
                * ``dlcrnet_stride32_ms5``
                * ``hrnet_w18``
                * ``hrnet_w32``
                * ``hrnet_w48``
                * ``resnet_101``
                * ``resnet_50``
                * ``rtmpose_m``
                * ``rtmpose_s``
                * ``rtmpose_x``
                * ``top_down_cspnext_m``
                * ``top_down_cspnext_s``
                * ``top_down_cspnext_x``
                * ``top_down_hrnet_w18``
                * ``top_down_hrnet_w32``
                * ``top_down_hrnet_w48``
                * ``top_down_resnet_101``
                * ``top_down_resnet_50``

    detector_type: string, optional, default=None
        Only for the PyTorch engine.
        When passing creating shuffles for top-down models, you can specify which
        detector you want. If the detector_type is None, the ```ssdlite``` will be used.
        The list of all available detectors can be obtained by calling
        ``deeplabcut.pose_estimation_pytorch.available_detectors()``. Supported options:
            * ``ssdlite``
            * ``fasterrcnn_mobilenet_v3_large_fpn``
            * ``fasterrcnn_resnet50_fpn_v2``

    augmenter_type: string, optional, default=None
        Type of augmenter. The options available depend on which engine is used.
        Currently supported options are:
            TensorFlow
                * ``default``
                * ``scalecrop``
                * ``imgaug``
                * ``tensorpack``
                * ``deterministic``
            PyTorch
                * ``albumentations``

    posecfg_template: string, optional, default=None
        Only for the TensorFlow engine.
        Path to a ``pose_cfg.yaml`` file to use as a template for generating the new
        one for the current iteration. Useful if you would like to start with the same
        parameters a previous training iteration. None uses the default
        ``pose_cfg.yaml``.

    superanimal_name: string, optional, default=""
        Only for the TensorFlow engine. For the PyTorch engine, use the ``weight_init``
        parameter.
        Specify the superanimal name is transfer learning with superanimal is desired.
        This makes sure the pose config template uses superanimal configs as template.

    weight_init: WeightInitialisation, optional, default=None
        PyTorch engine only. Specify how model weights should be initialized. The
        default mode uses transfer learning from ImageNet weights.

    engine: Engine, optional
        Whether to create a pose config for a Tensorflow or PyTorch model. Defaults to
        the value specified in the project configuration file. If no engine is specified
        for the project, defaults to ``deeplabcut.compat.DEFAULT_ENGINE``.

    ctd_conditions: int | str | Path | tuple[int, str] | tuple[int, int] | None, default = None,
        If using a conditional-top-down (CTD) net_type, this argument should be
        specified. It defines the conditions that will be used with the CTD model.
        It can be either:
            * A shuffle number (ctd_conditions: int), which must correspond to a
                bottom-up (BU) network type.
            * A predictions file path (ctd_conditions: string | Path), which must
                correspond to a .json or .h5 predictions file.
            * A shuffle number and a particular snapshot
                (ctd_conditions: tuple[int, str] | tuple[int, int]), which respectively
                correspond to a bottom-up (BU) network type and a particular snapshot
                name or index.

    Returns
    -------
    list(tuple) or None
        If training dataset was successfully created, a list of tuples is returned.
        The first two elements in each tuple represent the training fraction and the
        shuffle value. The last two elements in each tuple are arrays of integers
        representing the training and test indices.

        Returns None if training dataset could not be created.

    Notes
    -----
    Use the function ``add_new_videos`` at any stage of the project to add more videos
    to the project.

    Examples
    --------

    Linux/MacOS:
    >>> deeplabcut.create_training_dataset(
            '/analysis/project/reaching-task/config.yaml', num_shuffles=1,
        )

    >>> deeplabcut.create_training_dataset(
            '/analysis/project/reaching-task/config.yaml', Shuffles=[2], engine=deeplabcut.Engine.TF,
        )

    Windows:
    >>> deeplabcut.create_training_dataset(
            'C:\\Users\\Ulf\\looming-task\\config.yaml', Shuffles=[3,17,5],
        )
    """
    import scipy.io as sio

    if windows2linux:
        # DeprecationWarnings are silenced since Python 3.2 unless triggered in __main__
        warnings.warn(
            "`windows2linux` has no effect since 2.2.0.4 and will be removed in 2.2.1.",
            FutureWarning,
        )

    # Loading metadata from config file:
    cfg = auxiliaryfunctions.read_config(config)
    dlc_root_path = auxiliaryfunctions.get_deeplabcut_path()

    if superanimal_name != "":
        # FIXME(niels): this is deprecated
        supermodels = parse_available_supermodels()
        posecfg_template = os.path.join(
            dlc_root_path,
            "pose_estimation_tensorflow",
            "superanimal_configs",
            supermodels[superanimal_name],
        )

    if posecfg_template:
        if (
            not posecfg_template.endswith("pose_cfg.yaml")
            and not posecfg_template.endswith("superquadruped.yaml")
            and not posecfg_template.endswith("supertopview.yaml")
        ):
            raise ValueError(
                "posecfg_template argument must contain path to a pose_cfg.yaml file"
            )
        else:
            print("Reloading pose_cfg parameters from " + posecfg_template + "\n")
            from deeplabcut.utils.auxiliaryfunctions import read_plainconfig

        prior_cfg = read_plainconfig(posecfg_template)
    if cfg.get("multianimalproject", False):
        from deeplabcut.generate_training_dataset.multiple_individuals_trainingsetmanipulation import (
            create_multianimaltraining_dataset,
        )

        create_multianimaltraining_dataset(
            config,
            num_shuffles,
            Shuffles,
            net_type=net_type,
            detector_type=detector_type,
            trainIndices=trainIndices,
            testIndices=testIndices,
            userfeedback=userfeedback,
            engine=engine,
            weight_init=weight_init,
            ctd_conditions=ctd_conditions,
        )
    else:
        scorer = cfg["scorer"]
        project_path = cfg["project_path"]
        if engine is None:
            engine = compat.get_project_engine(cfg)

        # Create path for training sets & store data there
        trainingsetfolder = auxiliaryfunctions.get_training_set_folder(
            cfg
        )  # Path concatenation OS platform independent
        auxiliaryfunctions.attempt_to_make_folder(
            Path(os.path.join(project_path, str(trainingsetfolder))), recursive=True
        )

        # Create the trainset metadata file, if it doesn't yet exist
        if not metadata.TrainingDatasetMetadata.path(cfg).exists():
            trainset_metadata = metadata.TrainingDatasetMetadata.create(cfg)
            trainset_metadata.save()

        Data = merge_annotateddatasets(
            cfg,
            Path(os.path.join(project_path, trainingsetfolder)),
        )
        if Data is None:
            return
        Data = Data[scorer]  # extract labeled data

        # loading & linking pretrained models
        if net_type is None:  # loading & linking pretrained models
            net_type = cfg.get("default_net_type", "resnet_50")
        elif engine == Engine.PYTORCH:
            pass
        else:
            if (
                "resnet" in net_type
                or "mobilenet" in net_type
                or "efficientnet" in net_type
                or "dlcrnet" in net_type
            ):
                pass
            else:
                raise ValueError("Invalid network type:", net_type)

        top_down = False
        if engine == Engine.PYTORCH:
            if net_type.startswith("top_down_"):
                top_down = True
                net_type = net_type[len("top_down_") :]

        augmenters = compat.get_available_aug_methods(engine)
        default_augmenter = augmenters[0]
        if augmenter_type is None:
            augmenter_type = cfg.get("default_augmenter", default_augmenter)

            if augmenter_type is None:  # this could be in config.yaml for old projects!
                # updating variable if null/None! #backwardscompatability
                augmenter_type = default_augmenter
                auxiliaryfunctions.edit_config(
                    config, {"default_augmenter": augmenter_type}
                )
            elif augmenter_type not in augmenters:
                # as the default augmenter might not be available for the given engine
                augmenter_type = default_augmenter
                logging.info(
                    f"Default augmenter {augmenter_type} not available for engine "
                    f"{engine}: using {default_augmenter} instead"
                )

        if augmenter_type not in augmenters:
            if engine != Engine.PYTORCH:
                raise ValueError(
                    f"Invalid augmenter type: {augmenter_type} (available: for "
                    f"engine={engine}: {augmenters})"
                )

            logging.info(f"Switching augmentation to {default_augmenter} for PyTorch")
            augmenter_type = default_augmenter

        if posecfg_template:
            if net_type != prior_cfg["net_type"]:
                print(
                    "WARNING: Specified net_type does not match net_type from posecfg_template path entered. Proceed with caution."
                )
            if augmenter_type != prior_cfg["dataset_type"]:
                print(
                    "WARNING: Specified augmenter_type does not match dataset_type from posecfg_template path entered. Proceed with caution."
                )

        # Loading the encoder (if necessary downloading from TF)
        dlcparent_path = auxiliaryfunctions.get_deeplabcut_path()
        if not posecfg_template:
            defaultconfigfile = os.path.join(dlcparent_path, "pose_cfg.yaml")
        elif posecfg_template:
            defaultconfigfile = posecfg_template

        if engine == Engine.PYTORCH:
            model_path = dlcparent_path
        else:
            model_path = auxfun_models.check_for_weights(net_type, Path(dlcparent_path))

        Shuffles = validate_shuffles(cfg, Shuffles, num_shuffles, userfeedback)

        if trainIndices is None and testIndices is None:
            splits = [
                (
                    trainFraction,
                    shuffle,
                    SplitTrials(range(len(Data.index)), trainFraction),
                )
                for trainFraction in cfg["TrainingFraction"]
                for shuffle in Shuffles
            ]
        else:
            if len(trainIndices) != len(testIndices) != len(Shuffles):
                raise ValueError(
                    "Number of Shuffles and train and test indexes should be equal."
                )
            splits = []
            for shuffle, (train_inds, test_inds) in enumerate(
                zip(trainIndices, testIndices)
            ):
                trainFraction = round(
                    len(train_inds) * 1.0 / (len(train_inds) + len(test_inds)), 2
                )
                print(
                    f"You passed a split with the following fraction: {int(100 * trainFraction)}%"
                )
                # Now that the training fraction is guaranteed to be correct,
                # the values added to pad the indices are removed.
                train_inds = np.asarray(train_inds)
                train_inds = train_inds[train_inds != -1]
                test_inds = np.asarray(test_inds)
                test_inds = test_inds[test_inds != -1]
                splits.append(
                    (trainFraction, Shuffles[shuffle], (train_inds, test_inds))
                )

        bodyparts = auxiliaryfunctions.get_bodyparts(cfg)
        nbodyparts = len(bodyparts)
        for trainFraction, shuffle, (trainIndices, testIndices) in splits:
            if len(trainIndices) > 0:
                if userfeedback:
                    trainposeconfigfile, _, _ = compat.return_train_network_path(
                        config,
                        shuffle=shuffle,
                        trainingsetindex=cfg["TrainingFraction"].index(trainFraction),
                        engine=engine,
                    )
                    if trainposeconfigfile.is_file():
                        askuser = input(
                            "The model folder is already present. If you continue, it will overwrite the existing model (split). Do you want to continue?(yes/no): "
                        )
                        if (
                            askuser == "no"
                            or askuser == "No"
                            or askuser == "N"
                            or askuser == "No"
                        ):
                            raise Exception(
                                "Use the Shuffles argument as a list to specify a different shuffle index. Check out the help for more details."
                            )

                ####################################################
                # Generating data structure with labeled information & frame metadata (for deep cut)
                ####################################################
                # Make training file!
                (
                    datafilename,
                    metadatafilename,
                ) = auxiliaryfunctions.get_data_and_metadata_filenames(
                    trainingsetfolder, trainFraction, shuffle, cfg
                )

                ################################################################################
                # Saving data file (convert to training file for deeper cut (*.mat))
                ################################################################################
                data, MatlabData = format_training_data(
                    Data, trainIndices, nbodyparts, project_path
                )
                sio.savemat(
                    os.path.join(project_path, datafilename), {"dataset": MatlabData}
                )

                ################################################################################
                # Saving metadata (Pickle file)
                ################################################################################
                auxiliaryfunctions.save_metadata(
                    os.path.join(project_path, metadatafilename),
                    data,
                    trainIndices,
                    testIndices,
                    trainFraction,
                )
                metadata.update_metadata(
                    cfg=cfg,
                    train_fraction=trainFraction,
                    shuffle=shuffle,
                    engine=engine,
                    train_indices=trainIndices,
                    test_indices=testIndices,
                    overwrite=not userfeedback,
                )

                ################################################################################
                # Creating file structure for training &
                # Test files as well as pose_yaml files (containing training and testing information)
                #################################################################################
                modelfoldername = auxiliaryfunctions.get_model_folder(
                    trainFraction,
                    shuffle,
                    cfg,
                    engine=engine,
                )
                auxiliaryfunctions.attempt_to_make_folder(
                    Path(config).parents[0] / modelfoldername, recursive=True
                )
                auxiliaryfunctions.attempt_to_make_folder(
                    str(Path(config).parents[0] / modelfoldername) + "/train"
                )
                auxiliaryfunctions.attempt_to_make_folder(
                    str(Path(config).parents[0] / modelfoldername) + "/test"
                )

                path_train_config = str(
                    os.path.join(
                        cfg["project_path"],
                        Path(modelfoldername),
                        "train",
                        engine.pose_cfg_name,
                    )
                )
                path_test_config = str(
                    os.path.join(
                        cfg["project_path"],
                        Path(modelfoldername),
                        "test",
                        "pose_cfg.yaml",
                    )
                )
                if engine == Engine.TF:
                    items2change = {
                        "dataset": datafilename,
                        "engine": engine.aliases[0],
                        "metadataset": metadatafilename,
                        "num_joints": len(bodyparts),
                        "all_joints": [[i] for i in range(len(bodyparts))],
                        "all_joints_names": [str(bpt) for bpt in bodyparts],
                        "init_weights": model_path,
                        "project_path": str(cfg["project_path"]),
                        "net_type": net_type,
                        "dataset_type": augmenter_type,
                    }

                    items2drop = {}
                    if augmenter_type == "scalecrop":
                        # these values are dropped as scalecrop
                        # doesn't have rotation implemented
                        items2drop = {"rotation": 0, "rotratio": 0.0}
                    # Also drop maDLC smart cropping augmentation parameters
                    for key in [
                        "pre_resize",
                        "crop_size",
                        "max_shift",
                        "crop_sampling",
                    ]:
                        items2drop[key] = None

                    trainingdata = MakeTrain_pose_yaml(
                        items2change,
                        path_train_config,
                        defaultconfigfile,
                        items2drop,
                        save=(engine == Engine.TF),
                    )

                    keys2save = [
                        "dataset",
                        "num_joints",
                        "all_joints",
                        "all_joints_names",
                        "net_type",
                        "init_weights",
                        "global_scale",
                        "location_refinement",
                        "locref_stdev",
                    ]
                    MakeTest_pose_yaml(trainingdata, keys2save, path_test_config)
                    print(
                        "The training dataset is successfully created. Use the function"
                        "'train_network' to start training. Happy training!"
                    )
                elif engine == Engine.PYTORCH:
                    from deeplabcut.pose_estimation_pytorch.config.make_pose_config import (
                        make_pytorch_pose_config,
                        make_pytorch_test_config,
                    )
                    from deeplabcut.pose_estimation_pytorch.modelzoo.config import (
                        make_super_animal_finetune_config,
                    )

                    if weight_init is not None and weight_init.with_decoder:
                        pytorch_cfg = make_super_animal_finetune_config(
                            project_config=cfg,
                            pose_config_path=path_train_config,
                            model_name=net_type,
                            detector_name=detector_type,
                            weight_init=weight_init,
                            save=True,
                        )
                    else:
                        pytorch_cfg = make_pytorch_pose_config(
                            project_config=cfg,
                            pose_config_path=path_train_config,
                            net_type=net_type,
                            top_down=top_down,
                            detector_type=detector_type,
                            weight_init=weight_init,
                            save=True,
                            ctd_conditions=ctd_conditions,
                        )

                    make_pytorch_test_config(pytorch_cfg, path_test_config, save=True)

        return splits


def get_largestshuffle_index(config):
    """Returns the largest shuffle for all dlc-models in the current iteration."""
    shuffle_indices = get_existing_shuffle_indices(config)
    if len(shuffle_indices) > 0:
        return shuffle_indices[-1]

    return None


def get_existing_shuffle_indices(
    cfg: dict | str | Path,
    train_fraction: float | None = None,
    engine: Engine | None = None,
) -> List[int]:
    """
    Args:
        cfg: The content of a project configuration file, or the path to the project
            configuration file.
        train_fraction: If defined, only get the indices of shuffles with this train
            fraction.
        engine: If specified, returns only the shuffle indices that were created with
            the given engine. Can only be used when train_fraction is also defined.

    Returns:
        the indices of existing shuffles for this iteration of the project, sorted by
        ascending index
    """

    def is_valid_data_stem(stem: str) -> bool:
        if len(stem) == 0:
            return False
        suffix = stem.split("_")[-1]
        if len(suffix) == 0:
            return False
        info = suffix.split("shuffle")
        if len(info) != 2:
            return False
        train_frac, idx = info
        return (
            train_frac.isdigit()
            and idx.isdigit()
            and (train_fraction is None or int(train_frac) == int(100 * train_fraction))
        )

    if isinstance(cfg, (str, Path)):
        cfg = auxiliaryfunctions.read_config(cfg)

    project = Path(cfg["project_path"])
    trainset_folder = project / auxiliaryfunctions.get_training_set_folder(cfg)
    if not trainset_folder.exists():
        return []

    shuffle_indices = [
        int(p.stem.split("shuffle")[-1])
        for p in trainset_folder.iterdir()
        if (
            p.stem.startswith("Documentation_data")
            and p.suffix == ".pickle"
            and is_valid_data_stem(p.stem)
        )
    ]
    if engine is not None:
        if train_fraction is None:
            raise ValueError(
                f"Must select {train_fraction} to filter shuffles by engine"
            )

        shuffle_indices = [
            idx
            for idx in shuffle_indices
            if (
                project
                / auxiliaryfunctions.get_model_folder(
                    trainFraction=train_fraction,
                    shuffle=idx,
                    cfg=cfg,
                    engine=engine,
                )
            ).exists()
        ]

    return sorted(shuffle_indices)


def validate_shuffles(
    cfg: dict,
    shuffles: list[int] | None,
    num_shuffles: int | None,
    userfeedback: bool,
) -> list[int]:
    existing_shuffles = get_existing_shuffle_indices(cfg)
    if shuffles is None:
        first_index = 1
        if len(existing_shuffles) > 0:
            first_index = existing_shuffles[-1] + 1

        shuffles = range(first_index, num_shuffles + first_index)
    else:
        shuffles = [i for i in shuffles if isinstance(i, int)]
        for shuffle_idx in shuffles:
            if userfeedback and shuffle_idx in existing_shuffles:
                raise ValueError(
                    f"Cannot create shuffle {shuffle_idx} as it already exists - "
                    f"you must either create the dataset with `userfeedback=False` "
                    f"or delete the shuffle with index {shuffle_idx} manually (in "
                    f"`dlc-models`/`dlc-models-pytorch` and in the "
                    f"`training-datasets` folder) if you want to create a new "
                    f"shuffle with that index. You can otherwise create a shuffle "
                    f"with a new index. Existing indices are {existing_shuffles}."
                )

    return shuffles


def create_training_model_comparison(
    config,
    trainindex=0,
    num_shuffles=1,
    net_types=["resnet_50"],
    augmenter_types=["imgaug"],
    userfeedback=False,
    windows2linux=False,
):
    """Creates a training dataset to compare networks and augmentation types.

    The datasets are created such that the shuffles have same training and testing
    indices. Therefore, this function is useful for benchmarking the performance of
    different network and augmentation types on the same training/testdata.

    Parameters
    ----------
    config: str
        Full path of the config.yaml file.

    trainindex: int, optional, default=0
        Either (in case uniform = True) indexes which element of TrainingFraction in
        the config file should be used (note it is a list!).
        Alternatively (uniform = False) indexes which folder is dropped, i.e. the first
        if trainindex=0, the second if trainindex=1, etc.

    num_shuffles : int, optional, default=1
        Number of shuffles of training dataset to create,
        i.e. [1,2,3] for num_shuffles=3.

    net_types: list[str], optional, default=["resnet_50"]
        Currently supported networks are

        * ``"resnet_50"``
        * ``"resnet_101"``
        * ``"resnet_152"``
        * ``"mobilenet_v2_1.0"``
        * ``"mobilenet_v2_0.75"``
        * ``"mobilenet_v2_0.5"``
        * ``"mobilenet_v2_0.35"``
        * ``"efficientnet-b0"``
        * ``"efficientnet-b1"``
        * ``"efficientnet-b2"``
        * ``"efficientnet-b3"``
        * ``"efficientnet-b4"``
        * ``"efficientnet-b5"``
        * ``"efficientnet-b6"``

    augmenter_types: list[str], optional, default=["imgaug"]
        Currently supported augmenters are

        * ``"default"``
        * ``"imgaug"``
        * ``"tensorpack"``
        * ``"deterministic"``

    userfeedback: bool, optional, default=False
        If ``False``, then all requested train/test splits are created, no matter if
        they already exist. If you want to assure that previous splits etc. are not
        overwritten, then set this to True and you will be asked for each split.

    windows2linux

        ..deprecated::
            Has no effect since 2.2.0.4 and will be removed in 2.2.1.

    Returns
    -------
    shuffle_list: list
        List of indices corresponding to the trainingsplits/models that were created.

    Examples
    --------
    On Linux/MacOS

    >>> shuffle_list = deeplabcut.create_training_model_comparison(
            '/analysis/project/reaching-task/config.yaml',
            num_shuffles=1,
            net_types=['resnet_50','resnet_152'],
            augmenter_types=['tensorpack','deterministic'],
        )

    On Windows

    >>> shuffle_list = deeplabcut.create_training_model_comparison(
            'C:\\Users\\Ulf\\looming-task\\config.yaml',
            num_shuffles=1,
            net_types=['resnet_50','resnet_152'],
            augmenter_types=['tensorpack','deterministic'],
        )

    See ``examples/testscript_openfielddata_augmentationcomparison.py`` for an example
    of how to use ``shuffle_list``.
    """
    # read cfg file
    cfg = auxiliaryfunctions.read_config(config)

    if windows2linux:
        warnings.warn(
            "`windows2linux` has no effect since 2.2.0.4 and will be removed in 2.2.1.",
            FutureWarning,
        )

    # create log file
    log_file_name = os.path.join(cfg["project_path"], "training_model_comparison.log")
    logger = logging.getLogger("training_model_comparison")
    if not logger.handlers:
        logger = logging.getLogger("training_model_comparison")
        hdlr = logging.FileHandler(log_file_name)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        hdlr.setFormatter(formatter)
        logger.addHandler(hdlr)
        logger.setLevel(logging.INFO)
    else:
        pass

    existing_shuffles = get_existing_shuffle_indices(cfg)
    if len(existing_shuffles) == 0:
        largestshuffleindex = 0
    else:
        largestshuffleindex = existing_shuffles[-1] + 1

    shuffle_list = []
    for shuffle in range(num_shuffles):
        trainIndices, testIndices = mergeandsplit(
            config, trainindex=trainindex, uniform=True
        )
        for idx_net, net in enumerate(net_types):
            for idx_aug, aug in enumerate(augmenter_types):
                get_max_shuffle_idx = (
                    largestshuffleindex
                    + idx_aug
                    + idx_net * len(augmenter_types)
                    + shuffle * len(augmenter_types) * len(net_types)
                )

                shuffle_list.append(get_max_shuffle_idx)
                log_info = str(
                    "Shuffle index:"
                    + str(get_max_shuffle_idx)
                    + ", net_type:"
                    + net
                    + ", augmenter_type:"
                    + aug
                    + ", trainsetindex:"
                    + str(trainindex)
                    + ", frozen shuffle ID:"
                    + str(shuffle)
                )
                create_training_dataset(
                    config,
                    Shuffles=[get_max_shuffle_idx],
                    net_type=net,
                    trainIndices=[trainIndices],
                    testIndices=[testIndices],
                    augmenter_type=aug,
                    userfeedback=userfeedback,
                )
                logger.info(log_info)

    return shuffle_list


def create_training_dataset_from_existing_split(
    config: str,
    from_shuffle: int,
    from_trainsetindex: int = 0,
    num_shuffles: int = 1,
    shuffles: list[int] | None = None,
    userfeedback: bool = True,
    net_type: str | None = None,
    detector_type: str | None = None,
    augmenter_type: str | None = None,
    ctd_conditions: int | str | Path | tuple[int, str] | tuple[int, int] | None = None,
    posecfg_template: dict | None = None,
    superanimal_name: str = "",
    weight_init: WeightInitialization | None = None,
    engine: Engine | None = None,
) -> None | list[int]:
    """
    Labels from all the extracted frames are merged into a single .h5 file.
    Only the videos included in the config file are used to create this dataset.

    Args:
        config: Full path of the ``config.yaml`` file as a string.

        from_shuffle: The index of the shuffle from which to copy the train/test split.

        from_trainsetindex: The trainset index of the shuffle from which to use the data
            split. Default is 0.

        num_shuffles: Number of shuffles of training dataset to create, used if
            ``shuffles`` is None.

        shuffles: If defined, ``num_shuffles`` is ignored and a shuffle is created for
            each index given in the list.

        userfeedback: If ``False``, all requested train/test splits are created (no
            matter if they already exist). If you want to assure that previous splits
            etc. are not overwritten, set this to ``True`` and you will be asked for
            each existing split if you want to overwrite it.

        net_type: The type of network to create the shuffle for. Currently supported
            options for engine=Engine.TF are:
                * ``resnet_50``
                * ``resnet_101``
                * ``resnet_152``
                * ``mobilenet_v2_1.0``
                * ``mobilenet_v2_0.75``
                * ``mobilenet_v2_0.5``
                * ``mobilenet_v2_0.35``
                * ``efficientnet-b0``
                * ``efficientnet-b1``
                * ``efficientnet-b2``
                * ``efficientnet-b3``
                * ``efficientnet-b4``
                * ``efficientnet-b5``
                * ``efficientnet-b6``
            Currently supported  options for engine=Engine.TF can be obtained by calling
            ``deeplabcut.pose_estimation_pytorch.available_models()``.

        detector_type: string, optional, default=None
            Only for the PyTorch engine.
            When passing creating shuffles for top-down models, you can specify which
            detector you want. If the detector_type is None, the ```ssdlite``` will be
            used. The list of all available detectors can be obtained by calling
            ``deeplabcut.pose_estimation_pytorch.available_detectors()``. Supported
            options:
                * ``ssdlite``
                * ``fasterrcnn_mobilenet_v3_large_fpn``
                * ``fasterrcnn_resnet50_fpn_v2``

        augmenter_type: Type of augmenter. Currently supported augmenters for
            engine=Engine.TF are
                * ``default``
                * ``scalecrop``
                * ``imgaug``
                * ``tensorpack``
                * ``deterministic``
            The only supported augmenter for Engine.PYTORCH is ``albumentations``.

        posecfg_template: Only for Engine.TF. Path to a ``pose_cfg.yaml`` file to use as
            a template for generating the new one for the current iteration. Useful if
            you would like to start with the same parameters a previous training
            iteration. None uses the default ``pose_cfg.yaml``.

        superanimal_name: Specify the superanimal name is transfer learning with
            superanimal is desired. This makes sure the pose config template uses
            superanimal configs as template.

        weight_init: Only for Engine.PYTORCH. Specify how model weights should be
            initialized. The default mode uses transfer learning from ImageNet weights.

        engine: Whether to create a pose config for a Tensorflow or PyTorch model.
            Defaults to the value specified in the project configuration file. If no
            engine is specified for the project, defaults to
            ``deeplabcut.compat.DEFAULT_ENGINE``.

        ctd_conditions: int | str | Path | tuple[int, str] | tuple[int, int] | None, default = None,
            If using a conditional-top-down (CTD) net_type, this argument should be
            specified. It defines the conditions that will be used with the CTD model.
            It can be either:
                * A shuffle number (ctd_conditions: int), which must correspond to a
                  bottom-up (BU) network type.
                * A predictions file path (ctd_conditions: string | Path), which must
                  correspond to a .json or .h5 predictions file.
                * A shuffle number and a particular snapshot
                  (ctd_conditions: tuple[int, str] | tuple[int, int]), which
                  respectively correspond to a bottom-up (BU) network type and a
                  particular snapshot name or index.

    Returns:
        If training dataset was successfully created, a list of tuples is returned.
        The first two elements in each tuple represent the training fraction and the
        shuffle value. The last two elements in each tuple are arrays of integers
        representing the training and test indices.

        Returns None if training dataset could not be created.

    Raises:
        ValueError: If the shuffle from which to copy the data split doesn't exist.
    """
    cfg = auxiliaryfunctions.read_config(config)
    trainset_meta_path = metadata.TrainingDatasetMetadata.path(cfg)
    if not trainset_meta_path.exists():
        meta = metadata.TrainingDatasetMetadata.create(cfg)
        meta.save()
    else:
        meta = metadata.TrainingDatasetMetadata.load(cfg, load_splits=False)

    shuffle = meta.get(trainset_index=from_trainsetindex, index=from_shuffle)
    shuffle = shuffle.load_split(cfg, trainset_path=trainset_meta_path.parent)

    num_copies = num_shuffles
    if shuffles is not None:
        num_copies = len(shuffles)

    # pad the train and test indices with -1s so the training fraction is exact
    train_idx = list(shuffle.split.train_indices)
    test_idx = list(shuffle.split.test_indices)
    n_train, n_test = len(train_idx), len(test_idx)

    train_fraction = round(cfg["TrainingFraction"][from_trainsetindex], 2)
    if round(n_train / (n_train + n_test), 2) != train_fraction:
        train_padding, test_padding = _compute_padding(train_fraction, n_train, n_test)
        train_idx = train_idx + (train_padding * [-1])
        test_idx = test_idx + (test_padding * [-1])

    return create_training_dataset(
        config=config,
        num_shuffles=num_shuffles,
        Shuffles=shuffles,
        userfeedback=userfeedback,
        trainIndices=[train_idx for _ in range(num_copies)],
        testIndices=[test_idx for _ in range(num_copies)],
        net_type=net_type,
        detector_type=detector_type,
        augmenter_type=augmenter_type,
        posecfg_template=posecfg_template,
        superanimal_name=superanimal_name,
        weight_init=weight_init,
        engine=engine,
        ctd_conditions=ctd_conditions,
    )


def _compute_padding(
    train_fraction: float,
    num_train: int,
    num_test: int,
) -> tuple[int, int]:
    """
    Computes the amount of padding to add to train/test indices such that
    train_fraction = num_train / (num_train + num_test).

    Returns:
        the number of padding indices to add to the train indices
        the number of padding indices to add to the test indices
    """
    if train_fraction <= 0 or train_fraction >= 1:
        raise ValueError(
            f"The training fraction must satisfy 0 < TrainingFraction < 1, but "
            f"{train_fraction} was found"
        )

    base_images = 100
    train_step = int(round(round(train_fraction, 2) * base_images))
    test_step = base_images - train_step

    tgt_train = train_step
    tgt_test = test_step
    while tgt_train < num_train or tgt_test < num_test:
        tgt_train += train_step
        tgt_test += test_step

    return (tgt_train - num_train), (tgt_test - num_test)
