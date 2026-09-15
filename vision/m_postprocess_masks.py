import json
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage
from joblib import Parallel, delayed
from tqdm import tqdm

# Configuration
json_file = "/Users/sg2162/Datasets/CancerDatasets/CPTAC/Radiology_NIFTI/output_index.json"
data_root = "/Users/sg2162/Datasets/CancerDatasets/CPTAC/Radiology_NIFTI"
output_root = "/Users/sg2162/Datasets/CancerDatasets/CPTAC/Radiology_NIFTI_Post"

# json_file = "/Users/sg2162/Datasets/CancerDatasets/EAY131/Radiology_NIFTI/output_index.json"
# data_root = "/Users/sg2162/Datasets/CancerDatasets/EAY131/Radiology_NIFTI"
# output_root = "/Users/sg2162/Datasets/CancerDatasets/EAY131/Radiology_NIFTI_Post"

# Number of parallel jobs (-1 uses all CPU cores)
N_JOBS = 8  # Use all available cores
# Or set to specific number: N_JOBS = 8

def fill_holes_and_keep_largest(mask, dilation_iterations=2):
    """
    Process mask to fill holes and keep only the largest connected component.
    Uses scipy.ndimage exclusively.
    
    Args:
        mask: Binary mask (3D numpy array)
        dilation_iterations: Number of dilation iterations for hole filling
    
    Returns:
        Processed binary mask
    """
    # Ensure binary mask
    mask = mask > 0
    
    # Create structuring element for 3D (6-neighbor connectivity)
    struct_element = ndimage.generate_binary_structure(3, 1)
    
    # STEP 1: Fill holes using morphological closing
    # Dilation to close small gaps and fill holes
    dilated = ndimage.binary_dilation(mask, structure=struct_element, iterations=dilation_iterations)
    closed = ndimage.binary_erosion(dilated, structure=struct_element, iterations=dilation_iterations)
    
    # STEP 3: Keep only the largest connected component
    # Label connected components in the filled mask
    labeled_mask, num_components = ndimage.label(closed, structure=struct_element)
    
    if num_components == 0:
        return np.zeros_like(mask, dtype=bool)
    
    # Get sizes of all components
    component_sizes = np.bincount(labeled_mask.ravel())
    
    # Skip background (label 0)
    if len(component_sizes) > 1:
        # Find the largest component label (excluding background)
        largest_label = np.argmax(component_sizes[1:]) + 1
        # Create mask with only the largest component
        final_mask = (labeled_mask == largest_label)
    else:
        final_mask = np.zeros_like(mask, dtype=bool)
    
    return final_mask


def process_single_case(item, patient_id, data_root, output_root, dilation_iterations=2):
    """
    Process a single case: read NIfTI, process mask, save processed mask.
    This function is designed to be called by joblib.
    
    Args:
        item: Dictionary containing image and label paths
        patient_id: Patient ID string
        data_root: Root directory for input data
        output_root: Root directory for output data
        dilation_iterations: Number of dilation iterations for hole filling
    
    Returns:
        Tuple of (status, patient_id, label_path, message)
    """
    try:
        # Construct paths
        image_path = Path(data_root) / item["image"]
        label_path = Path(data_root) / item["label"]
        
        # Construct output path (preserve directory structure)
        rel_path = Path(item["label"])
        output_path = Path(output_root) / rel_path
        
        # Load NIfTI files
        mask_nii = nib.load(str(label_path))
        
        # Get mask data
        mask_data = mask_nii.get_fdata() > 0
        
        # Check if mask has any positive values
        if np.sum(mask_data) == 0:
            # Save empty mask
            output_nii = nib.Nifti1Image(
                np.zeros_like(mask_data, dtype=np.uint8),
                mask_nii.affine,
                mask_nii.header
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            nib.save(output_nii, str(output_path))
            return ('empty', patient_id, str(rel_path), 'Empty mask')
        
        # Process mask
        processed_mask = fill_holes_and_keep_largest(
            mask_data, 
            dilation_iterations=dilation_iterations
        )
        
        # Check if processed mask is empty
        if np.sum(processed_mask) == 0:
            status = 'empty_after_processing'
        else:
            status = 'success'
        
        # Save processed mask
        output_nii = nib.Nifti1Image(
            processed_mask.astype(np.uint8),
            mask_nii.affine,
            mask_nii.header
        )
        
        # Create output directory if it doesn't exist
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        nib.save(output_nii, str(output_path))
        
        return (status, patient_id, str(rel_path), 'OK')
        
    except Exception as e:
        return ('error', patient_id, str(label_path), str(e))


def main():
    # Load the index file
    print("Loading dataset index...")
    with open(json_file) as f:
        dataset = json.load(f)
    
    # Flatten the dataset into a list of (patient_id, item) tuples
    tasks = []
    for patient_id, items in dataset.items():
        for item in items:
            tasks.append((patient_id, item))
    
    total_cases = len(tasks)
    print(f"Total cases to process: {total_cases}")
    print(f"Using {N_JOBS if N_JOBS > 0 else 'all'} CPU cores")
    print("Starting parallel processing...")
    
    # Process in parallel using joblib
    results = Parallel(n_jobs=N_JOBS, verbose=10)(
        delayed(process_single_case)(
            item, 
            patient_id, 
            data_root, 
            output_root, 
            dilation_iterations=2
        )
        for patient_id, item in tasks
    )
    
    # Process results and collect statistics
    stats = {
        'success': 0,
        'empty': 0,
        'empty_after_processing': 0,
        'error': 0
    }
    
    error_messages = []
    
    print("\n" + "="*60)
    print("Processing Complete! Summary:")
    print("="*60)
    
    for status, patient_id, path, message in results:
        stats[status] = stats.get(status, 0) + 1
        
        if status == 'error':
            error_messages.append(f"  {patient_id}: {path} - {message}")
    
    print(f"✓ Successfully processed: {stats['success']}")
    print(f"○ Empty masks (skipped): {stats['empty']}")
    print(f"⚠ Empty after processing: {stats['empty_after_processing']}")
    print(f"✗ Errors: {stats['error']}")
    print(f"\nTotal cases: {total_cases}")
    
    if error_messages:
        print("\n" + "="*60)
        print("Error Details:")
        print("="*60)
        for msg in error_messages:
            print(msg)
    
    print(f"\nOutput directory: {output_root}")
    print("="*60)


if __name__ == "__main__":
    main()