# STAR
Spatial Tumor Anatomical Reasoning

## Run the annotation app

The web app expects a data directory with dataset folders containing rendered
step-1 images:

```text
STAR_DATA/
  CPTAC/vis_check/*.png
  EAY131/vis_check/*.png
  R2Seg/vis_check/*.png
```

Start the app with:

```bash
python3 web/app.py --data_dir /path/to/STAR_DATA
```

When `manifests/images.json` already exists, `--data_dir` does not rebuild or
reshuffle it. The app keeps the existing image IDs and order, and only replaces
the old data root in each image path with the provided data directory.

If `--data_dir` is omitted, the app uses the original local default:
`/Users/sg2162/Datasets/CancerDatasets/STAR`.
