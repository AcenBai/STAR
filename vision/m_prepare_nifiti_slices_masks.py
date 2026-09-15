import json
from pathlib import Path

import cv2
import nibabel as nib
import numpy as np
import pandas as pd
from skimage.transform import resize
from scipy.ndimage import binary_dilation
from scipy import ndimage
from joblib import Parallel, delayed
from nibabel.orientations import (
    io_orientation,
    axcodes2ornt,
    ornt_transform,
    apply_orientation,
    inv_ornt_aff,
)

# Dataset configuration
dataset = "R2Seg"  # Change this to "R2Seg", "CPTAC", or "EAY131"
N_JOBS = 8

if dataset == "R2Seg":
    json_file = None  # Will be handled differently for R2Seg
    data_root = "/Users/sg2162/Datasets/TumorSegmentation"
    output_root = "/Users/sg2162/Datasets/CancerDatasets/STAR/R2Seg/vis_check"
    metadata_dir = None
    trackingID = None
    r2seg_mode = True
elif dataset == "EAY131":
    json_file = "/Users/sg2162/Datasets/CancerDatasets/EAY131/Radiology_NIFTI/output_index.json"
    data_root = "/Users/sg2162/Datasets/CancerDatasets/EAY131/Radiology_NIFTI"
    output_root = "/Users/sg2162/Datasets/CancerDatasets/STAR/EAY131/vis_check"
    metadata_dir = "/Users/sg2162/Datasets/CancerDatasets/EAY131/Radiology/Metadata"
    trackingID = "TrackingID"
    r2seg_mode = False
elif dataset == "CPTAC":
    json_file = "/Users/sg2162/Datasets/CancerDatasets/CPTAC/Radiology_NIFTI/output_index.json"
    data_root = "/Users/sg2162/Datasets/CancerDatasets/CPTAC/Radiology_NIFTI"
    output_root = "/Users/sg2162/Datasets/CancerDatasets/STAR/CPTAC/vis_check"
    metadata_dir = "/Users/sg2162/Datasets/CancerDatasets/CPTAC/Metadata"
    trackingID = "Tracking ID"
    r2seg_mode = False

def get_largest_component_center(mask):
    """
    Return the voxel center (centroid) of the largest connected component.

    Parameters
    ----------
    mask : ndarray
        Binary mask.

    Returns
    -------
    ndarray of shape (3,) or None
        Integer voxel coordinates (z, y, x) of the centroid,
        or None if the mask is empty.
    """
    labeled, num = ndimage.label(mask > 0)

    if num == 0:
        return None

    # Ignore background (label 0)
    sizes = np.bincount(labeled.ravel())
    largest_label = np.argmax(sizes[1:]) + 1

    center = ndimage.center_of_mass(mask, labeled, largest_label)

    return np.round(center).astype(int)


def normalize(img, msk=None, modality='auto'):
    """
    Normalize image based on modality and mask statistics.
    
    Args:
        img: Input image (2D or 3D)
        msk: Binary mask (same shape as img)
        modality: 'MR', 'CT', or 'auto' (auto-detect)
    
    Returns:
        Normalized uint8 image
    """
    
    # CT: Use mask-based adaptive windowing
    if modality == 'CT':
        return normalize_ct_local_context(img, msk)
    else:
        l, u = np.percentile(img, [0.5, 99.5])
        img = np.clip(img, l, u)
        img = (img - img.min()) / (img.max() - img.min() + 1e-8)
        return (img * 255).astype(np.uint8)


def normalize_ct_local_context(
    img,
    msk=None,
    dilation_iter=30,
    hu_min=-700,
    hu_max=1200,
    p_low=0.5,
    p_high=99.5,
    padding_ratio=0.10,
):
    """
    CT normalization using local anatomical context.

    Parameters
    ----------
    img : ndarray
        CT image in HU.
    msk : ndarray
        Binary tumor mask.
    dilation_iter : int
        Number of dilation iterations (recommend using a physical-distance
        dilation if voxel spacing is available).
    hu_min : int
        Lower HU bound used when estimating statistics.
        Removes air/background.
    hu_max : int
        Upper HU bound used when estimating statistics.
        Removes metal and extreme artifacts.
    p_low, p_high : float
        Robust percentile range.
    padding_ratio : float
        Expand the estimated window slightly.

    Returns
    -------
    uint8 image in [0,255]
    """

    # -------------------------------
    # Fallback
    # -------------------------------
    if msk is None or np.sum(msk) == 0:
        return normalize_ct_robust(img)

    # -------------------------------
    # Local anatomical context
    # -------------------------------
    context = binary_dilation(msk.astype(bool),
                              iterations=dilation_iter)

    values = img[
        context &
        (img > hu_min) &
        (img < hu_max)
    ]

    # Safety fallback
    if values.size < 100:
        return normalize_ct_robust(img)

    # -------------------------------
    # Robust adaptive window
    # -------------------------------
    lower = np.percentile(values, p_low)
    upper = np.percentile(values, p_high)

    # Avoid pathological narrow windows
    if upper - lower < 200:
        center = (upper + lower) / 2
        lower = center - 100
        upper = center + 100

    # Slightly enlarge window
    pad = padding_ratio * (upper - lower)
    lower -= pad
    upper += pad

    # -------------------------------
    # Normalize whole image
    # -------------------------------
    img = np.clip(img, lower, upper)
    img = (img - lower) / (upper - lower + 1e-8)

    return (img * 255).astype(np.uint8)


def normalize_ct_robust(
    img,
    hu_min=-700,
    hu_max=1200,
    p_low=0.5,
    p_high=99.5,
    padding_ratio=0.10,
    ):
    """
    Fallback: robust percentile normalization for CT when no mask is available.
    """
    values = img[
        (img > hu_min) &
        (img < hu_max)
    ]

    # -------------------------------
    # Robust adaptive window
    # -------------------------------
    lower = np.percentile(values, p_low)
    upper = np.percentile(values, p_high)

    # Avoid pathological narrow windows
    if upper - lower < 200:
        center = (upper + lower) / 2
        lower = center - 100
        upper = center + 100

    # Slightly enlarge window
    pad = padding_ratio * (upper - lower)
    lower -= pad
    upper += pad

    # -------------------------------
    # Normalize whole image
    # -------------------------------
    img = np.clip(img, lower, upper)
    img = (img - lower) / (upper - lower + 1e-8)

    return (img * 255).astype(np.uint8)


def resize_keep_aspect(img, target_size=256):
    h, w = img.shape[:2]

    scale = target_size / max(h, w)

    nh = int(h * scale)
    nw = int(w * scale)

    resized = cv2.resize(
        img,
        (nw, nh),
        interpolation=cv2.INTER_AREA
    )

    if img.ndim == 2:
        canvas = np.zeros((target_size, target_size), dtype=np.uint8)
    else:
        canvas = np.zeros((target_size, target_size, 3), dtype=np.uint8)

    y0 = (target_size - nh) // 2
    x0 = (target_size - nw) // 2

    canvas[y0:y0+nh, x0:x0+nw] = resized

    return canvas


def resize_to_isotropic(slice2d, spacing_y, spacing_x, order=1):
    """
    Resample a 2D slice so displayed pixels are isotropic.

    spacing_y -> row direction spacing
    spacing_x -> column direction spacing
    """

    h, w = slice2d.shape

    new_h = int(round(h * spacing_y))
    new_w = int(round(w * spacing_x))

    return resize(
        slice2d,
        (new_h, new_w),
        order=order,
        preserve_range=True,
        anti_aliasing=(order > 0)
    )


def extract_views_isotropic(image, mask, center, spacing):

    x, y, z = center

    sx, sy, sz = spacing

    views = []

    #
    # Sagittal (YZ plane)
    #
    img_slice = np.rot90(image[x, :, :])
    mask_slice = np.rot90(mask[x, :, :])

    img_slice = resize_to_isotropic(
        img_slice,
        sz,   # rows = Z
        sy,   # cols = Y
        order=1
    )

    mask_slice = resize_to_isotropic(
        mask_slice.astype(float),
        sz,
        sy,
        order=0
    ) > 0.5

    views.append((img_slice, mask_slice))

    #
    # Coronal (XZ plane)
    #
    img_slice = np.rot90(image[:, y, :])
    mask_slice = np.rot90(mask[:, y, :])

    img_slice = resize_to_isotropic(
        img_slice,
        sz,   # rows = Z
        sx,   # cols = X
        order=1
    )

    mask_slice = resize_to_isotropic(
        mask_slice.astype(float),
        sz,
        sx,
        order=0
    ) > 0.5

    views.append((img_slice, mask_slice))

    #
    # Axial (XY plane)
    #
    img_slice = np.rot90(image[:, :, z])
    mask_slice = np.rot90(mask[:, :, z])

    img_slice = resize_to_isotropic(
        img_slice,
        sy,   # rows = Y
        sx,   # cols = X
        order=1
    )

    mask_slice = resize_to_isotropic(
        mask_slice.astype(float),
        sy,
        sx,
        order=0
    ) > 0.5

    views.append((img_slice, mask_slice))

    return views

def make_overlay(img, mask):

    rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    red = np.zeros_like(rgb)
    red[..., 2] = 255

    idx = mask > 0

    rgb[idx] = (
        0.5 * rgb[idx] +
        0.5 * red[idx]
    ).astype(np.uint8)

    return rgb


def load_tracking_lookup(metadata_dir):
    lookup = {}

    if metadata_dir is None:
        return lookup

    metadata_dir = Path(metadata_dir)

    for csv_file in metadata_dir.glob("*.csv"):

        try:
            df = pd.read_csv(csv_file, dtype=str)

            if (
                "SeriesInstanceUID" not in df.columns
                or trackingID not in df.columns
            ):
                continue

            for _, row in df.iterrows():

                series_uid = str(row["SeriesInstanceUID"]).strip()
                tracking_id = str(row[trackingID]).strip()

                if series_uid and tracking_id:
                    lookup[series_uid] = tracking_id

        except Exception as e:
            print(f"Failed loading metadata: {csv_file}")
            print(e)

    print(f"Loaded {len(lookup)} SeriesInstanceUID mappings")

    return lookup

def load_modality_lookup(metadata_dir):
    """
    Load modality information from CSV files.
    Assumes CSV has columns: SeriesInstanceUID, ReferencedSeriesModality
    """
    modality_lookup = {}
    metadata_dir = Path(metadata_dir)

    for csv_file in metadata_dir.glob("*.csv"):
        try:
            df = pd.read_csv(csv_file, dtype=str)
            
            # Check for required columns
            if "SeriesInstanceUID" not in df.columns or "ReferencedSeriesModality" not in df.columns:
                continue

            for _, row in df.iterrows():
                series_uid = str(row["SeriesInstanceUID"]).strip()
                modality = str(row["ReferencedSeriesModality"]).strip()
                
                if series_uid and modality:
                    modality_lookup[series_uid] = modality

        except Exception as e:
            print(f"Failed loading modality from: {csv_file}")
            print(e)

    print(f"Loaded modality for {len(modality_lookup)} series")
    return modality_lookup

def save_qc(image, mask, spacing, modality, save_path):

    center = get_largest_component_center(mask)

    if center is None:
        return

    views = extract_views_isotropic(
        image,
        mask,
        center,
        spacing
    )

    top_row = []
    bottom_row = []

    for img_slice, mask_slice in views:

        img_slice = normalize(img_slice, None, modality)

        top_row.append(
            resize_keep_aspect(img_slice, 256)
        )

        overlay = make_overlay(
            img_slice,
            mask_slice
        )

        bottom_row.append(
            resize_keep_aspect(overlay, 256)
        )

    top_row = np.hstack(top_row)
    bottom_row = np.hstack(bottom_row)

    canvas = np.vstack([
        cv2.cvtColor(top_row, cv2.COLOR_GRAY2BGR),
        bottom_row
    ])

    save_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    cv2.imwrite(
        str(save_path),
        canvas,
        [cv2.IMWRITE_PNG_COMPRESSION, 1]
    )

def reorient_to_lps(img_nii):
    """
    Reorient a NIfTI image to LPS (Left-Posterior-Superior) orientation.
    """
    # Current orientation
    current_ornt = io_orientation(img_nii.affine)

    # Target orientation: LPS
    target_ornt = axcodes2ornt(("L", "P", "S"))

    # Compute orientation transform
    transform = ornt_transform(current_ornt, target_ornt)

    # Reorient image data
    data_lps = apply_orientation(img_nii.get_fdata(), transform)

    # Update affine
    affine_lps = img_nii.affine @ inv_ornt_aff(transform, img_nii.shape)

    return nib.Nifti1Image(data_lps, affine_lps, img_nii.header)

def process_case_r2seg(item, data_root, output_root):
    """Process R2Seg case from dataset.json structure"""
    
    image_rel_path = item["image"]
    label_rel_path = item["label"]
    folder_name = item["folder"]
    
    # Construct full paths
    image_path = Path(data_root) / folder_name / image_rel_path
    label_path = Path(data_root) / folder_name / label_rel_path
    
    try:
        img_nii = nib.load(str(image_path))
        mask_nii = nib.load(str(label_path))

        # Reorient both to LPS
        img_nii = reorient_to_lps(img_nii)
        mask_nii = reorient_to_lps(mask_nii)

        image = img_nii.get_fdata(dtype=np.float32)
        mask_data = mask_nii.get_fdata()
        
        # Extract mask for tumor label (default to label 1)
        # Get tumor label from the item if available, otherwise use 1
        tumor_label = item.get("tumor_label", 1)
        mask = mask_data == tumor_label

        spacing = img_nii.header.get_zooms()[:3]

        # Get modality from dataset or item
        modality = item.get("modality", "MR")
        # If modality is stored as a dict (e.g., {"0": "MR"}), extract the value
        if isinstance(modality, dict):
            modality = list(modality.values())[0] if modality else "MR"

        # For R2Seg, use the folder name and image name for output
        # e.g., Bladder_Tumor_00/Center1_007
        base_name = image_path.name.replace(".nii.gz", "")
        tumor_site = folder_name.split("_")[0]
        
        save_name = f"{folder_name}_{base_name}-site_in_{tumor_site}.png"
        save_path = Path(output_root) / save_name
        
        save_qc(
            image=image,
            mask=mask,
            spacing=spacing,
            modality=modality,
            save_path=save_path,
        )

        return f"OK: {save_name} (modality: {modality})"

    except Exception as e:
        return f"FAILED: {label_path}\n{e}"


def process_case_cptac_eay131(item, data_root, output_root, tracking_lookup, modality_lookup):
    """Original processing for CPTAC and EAY131"""
    
    image_path = Path(data_root) / item["image"]
    label_path = Path(data_root + "_Post") / item["label"]

    try:
        img_nii = nib.load(str(image_path))
        mask_nii = nib.load(str(label_path))

        # Reorient both to RAS
        img_nii = reorient_to_lps(img_nii)
        mask_nii = reorient_to_lps(mask_nii)

        image = img_nii.get_fdata(dtype=np.float32)
        mask = mask_nii.get_fdata() > 0

        spacing = img_nii.header.get_zooms()[:3]

        rel_path = Path(item["label"])

        parts = list(rel_path.parts[-2:-1])

        # case UID is the last directory before the file
        case_uid = parts[-1]

        tracking_id = tracking_lookup.get(case_uid)
        modality = modality_lookup.get(case_uid, "unknown")

        if tracking_id:
            parts.append('site_in_' + tracking_id)
        else:
            parts.append(
                rel_path.name.replace(".nii.gz", "")
            )

        save_name = "-".join(parts) + ".png"

        save_path = Path(output_root) / save_name

        save_qc(
            image=image,
            mask=mask,
            spacing=spacing,
            modality=modality,
            save_path=save_path,
        )

        return f"OK: {save_name}"

    except Exception as e:
        return f"FAILED: {label_path}\n{e}"


def load_r2seg_tasks(data_root):
    """Load all tasks from R2Seg dataset.json files"""
    tasks = []
    data_root_path = Path(data_root)
    
    # Find all dataset.json files in subdirectories
    for dataset_json in data_root_path.rglob("dataset.json"):
        try:
            with open(dataset_json) as f:
                data = json.load(f)
            
            # Get the folder name (e.g., Bladder_Tumor_00)
            folder_name = dataset_json.parent.name
            
            # Extract modality and label information
            modality = data.get("modality", {})
            if isinstance(modality, dict):
                # Convert dict like {"0": "MR"} to a string
                modality_str = list(modality.values())[0] if modality else "unknown"
            else:
                modality_str = modality
            
            # Get tumor label from labels dict
            labels = data.get("labels", {})
            tumor_label = None
            for key, value in labels.items():
                if value.lower() == "tumor":
                    tumor_label = int(key)
                    break
            
            # If tumor label not found, default to 1
            if tumor_label is None:
                tumor_label = 1
                print(f"Warning: No 'tumor' label found in {dataset_json}, defaulting to label 1")
            
            # Process both train and test splits
            for split in ["train", "test"]:
                if split in data:
                    for item in data[split]:
                        # Add folder name, modality, and tumor label to track which dataset it came from
                        item["folder"] = folder_name
                        item["modality"] = modality_str
                        item["tumor_label"] = tumor_label
                        tasks.append(item)
            
            print(f"Loaded {len(data.get('train', [])) + len(data.get('test', []))} tasks from {dataset_json}")
            print(f"  Modality: {modality_str}, Tumor label: {tumor_label}")
            
        except Exception as e:
            print(f"Error loading {dataset_json}: {e}")
    
    print(f"Total R2Seg tasks: {len(tasks)}")
    return tasks


# ------------------------------------------------------
# Main execution
# ------------------------------------------------------

if r2seg_mode:
    # R2Seg mode - load tasks from dataset.json files
    tasks = load_r2seg_tasks(data_root)
    
    if not tasks:
        print("No tasks found for R2Seg!")
    else:
        results = Parallel(
            n_jobs=N_JOBS,
            backend="loky",
            verbose=10,
        )(
            delayed(process_case_r2seg)(
                item,
                data_root,
                output_root,
            )
            for item in tasks
        )
else:
    # CPTAC or EAY131 mode - load from single JSON file
    with open(json_file) as f:
        dataset_data = json.load(f)

    tracking_lookup = load_tracking_lookup(metadata_dir)
    modality_lookup = load_modality_lookup(metadata_dir)

    tasks = []

    for _, items in dataset_data.items():
        tasks.extend(items)
    
    results = Parallel(
        n_jobs=N_JOBS,
        backend="loky",
        verbose=10,
    )(
        delayed(process_case_cptac_eay131)(
            item,
            data_root,
            output_root,
            tracking_lookup,
            modality_lookup,
        )
        for item in tasks
    )

# ------------------------------------------------------
# Print results
# ------------------------------------------------------

for r in results:
    print(r)