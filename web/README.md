# STAR annotation web app

Run from the project root:

```bash
python3 web/app.py
```

Then open:

```text
http://127.0.0.1:8765
```

The first launch creates `manifests/images.json` from the current CPTAC and EAY131
`vis_check` PNG folders. The manifest order is deterministically shuffled with a
fixed project seed, so different users see the same shuffled image sequence.

Outputs:

- `users/{user_id}.json`: user type, resume state, completed image IDs.
- `answers/{user_id}.jsonl`: one autosaved answer event per submitted stage.

## Back within the current image

Q2, Q3, and Q4 have a Back button that returns exactly one question within the
current image. Q1 and completed images cannot go back. Submitted selections are
prefilled when revisited. Changing Q1 or Q2 clears dependent later answers;
unchanged answers remain available for reconfirmation. Submitting Q4 completes
the image once and advances to the next assigned image or study phase.

`POST /api/back` uses the same `user_id`, `image_id`, `stage`, and optional
`task_revision` as `POST /api/answer`. Current clients send the revision to reject
outdated requests from other tabs; older clients remain compatible. Per-user
locks serialize answer, back, resume, and current-task requests.

The append-only answer log includes `stage: "back"` navigation events and any
resubmitted stage events. Existing records are preserved. Count progress and
accuracy using `stage: "complete"` records (one final record per image), not
total log lines divided by four. Revisiting earlier questions does not add
completed images or accuracy observations.

## Image reload and delivery

`Reload image` requests only the current image with a unique `reload` query
parameter to bypass its previous browser cache entry. It never reloads the page,
calls a task API, changes the selected answer, or saves an answer/navigation event.
It is available while an image is loading or has failed, with no retry limit.
Submitting answers remains available during image loading. A replaced image owns
its event handlers, so a late load/error event cannot affect the next task image.

Original PNGs, dimensions, opaque image IDs, assignment manifests, and answer data
are unchanged. Image responses use `Cache-Control: private, max-age=3600`, ETag, and
Last-Modified. API responses and other files do not receive this cache policy.

Standalone Python delivery is the default. To offload file transmission to Nginx:

1. Install `ops/nginx/star-image-logging.conf` in `/etc/nginx/conf.d/` (http
   context), then adapt/install `ops/nginx/star.conf` as the site's configuration,
   preserving the existing server/proxy settings. Validate with `nginx -t` before
   a graceful reload.
2. Verify the Nginx worker can read the existing `/srv/star-data/` PNGs.
3. Install `ops/nginx/star-image-offload.conf` as
   `/etc/systemd/system/star.service.d/image-offload.conf`, reload systemd, and
   restart the application during a quiet interval.

Python validates the opaque ID and resolved PNG path, then uses X-Accel-Redirect
to the internal `/_star_image_files/` location. Nginx serves PNG files,
handles conditional requests/ranges, and consumes the internal filename header.
Direct browser requests to this location must return 404; filenames/GT labels
must not appear in public response headers. No proxy cache is enabled.

Rollback: unset/remove the offload environment override and restart Python to
restore standalone image delivery. The internal Nginx location can remain until
its previous site configuration is restored and smoothly reloaded. Do not restore
or reset user/answer/assignment files when rolling back program code.

### Optional lossless PNG cache

Build this once against the frozen image dataset, at low CPU priority. It uses
only the Python standard library; no image processing dependency runs in the app:

```bash
nice -n 19 python3 -B ops/images/prepare_png_cache.py \
  --manifest /opt/STAR/manifests/images.json --data-dir /srv/star-data \
  --cache-root /var/cache/star-images \
  --report /opt/monitor/star-png-cache-report.json
```

The generator checks every PNG chunk CRC, recompresses only the IDAT zlib stream,
and checks that decompression yields exactly the same filtered image bytes. All
other chunks, including dimensions, color information and alpha representation,
are preserved. It verifies each written file and source stability before declaring
success. Only smaller copies are retained, with one cache path per source image.
Original files are never rewritten. The report includes original, delivered and
filtered-stream SHA-256 hashes for each image.

Nginx checks `/var/cache/star-images/_star_image_files/` first and falls back to
the original PNG if no cached file exists. Both locations are internal; public
opaque image URLs and task APIs stay unchanged. ETag, conditional requests,
Range/If-Range and the manual `Reload image` button work for either source. An
old original-file ETag cannot resume into the different compressed byte stream.
No page refresh, application restart, automatic retry or task prefetch is needed.

Treat these copies as disposable derived data, excluded from data backups and
Git. Regenerate or discard affected copies before changing the frozen originals.
To disable the cache, restore the previous Nginx site configuration and gracefully
reload; doctors' data and the Python service do not need to change.

`/var/log/nginx/star-images.log` records only opaque image requests, elapsed time,
sent/declared byte counts, completion, upstream time and range headers. It records
no IP addresses or answer fields. Keep it under the existing Nginx log rotation
policy. These are server-side transfer observations; a successful server write
alone does not prove that the browser decoded or displayed the whole image.

### Browser diagnostics and one-image lookahead

`public_task` includes `next_image` with only the next opaque ID and image URL.
The lookahead function copies the completion list in memory to determine what
would follow the current image. It never saves that copy, advances the real user,
or writes answer events. It follows the human manifest, frozen MLLM split and each
radiologist's frozen group, including Phase 1 -> Phase 2/3 boundaries.

The browser waits until the displayed image has loaded, then waits 250 ms and
downloads exactly one next image with low fetch priority. Loading that hidden
image never starts another prefetch. On a matching next task it promotes the same
image node, including an in-flight request, instead of starting a second download.
A different account, manual reload or study completion cancels obsolete lookahead.
Prefetch errors fall back to ordinary loading when that image becomes current.
No automatic image retry or submission gate is added. Answer selections and task
revisions are not part of the image transport module.

Browser reports distinguish `display`, `reload` and `prefetch`, with outcomes
`loaded`, `error`, `cancelled` and a single `slow` observation after 15 seconds.
A slow observation leaves the request running. A prefetched request and its later
display have separate observations; do not sum them as independent downloads.
`prefetch_used` identifies promotion of a ready or in-flight image. Elapsed time
measures that observation; available Resource Timing fields describe the resource
transfer (which may have begun earlier during prefetch). Zero browser byte counts
can mean cache reuse or unavailable information, rather than an empty image.

Reports use a best-effort beacon/fetch to `/api/image-metrics`, outside the task API
flow. They include account ID, opaque image ID, observation ID, browser/server
timestamps and bounded timing/byte fields. They contain no choices, answers,
ground truth, original filenames or IP addresses. Logging failures are ignored.
The endpoint never loads or saves a user or answer record. It rejects bodies over
4096 bytes and drops unknown fields.

To enable persistent diagnostics, install `ops/nginx/star-image-metrics.conf` as
`/etc/systemd/system/star.service.d/image-metrics.conf` and restart the app during
a quiet interval. It sets `STAR_IMAGE_METRICS_LOG=/opt/monitor/star-image-client.jsonl`.
Install `ops/nginx/star-image-client.logrotate` as `/etc/logrotate.d/star-image-client`:
daily rotation retains five rotated logs, with compression. The app also caps the
active diagnostic log at 10 MiB; after the cap it drops diagnostics until rotation.
Without the environment setting, valid reports are accepted and discarded.
Existing Nginx image timings remain at `/var/log/nginx/star-images.log`.

Validate against disposable users only:

```bash
node --test tests/image_reload.test.cjs tests/image_prefetch.test.cjs
python3 -B -m unittest discover -s tests -v
```

Rollback code/config only. Keep the user, answer, assignment, manifest, original
image and PNG-cache files intact. Previously opened pages continue to work; they
receive the new browser code after the user opens a new page or safely refreshes.

## MLLM benchmark

Generate the frozen MLLM benchmark definition with:

```bash
python3 web/generate_mllm_split.py
python3 web/generate_mllm_prompts.py
```

`assignments/mllm_split.json` contains all 10,565 images that sg2162 marked
`No issue` for Q4, in filtered manifest order, plus source hashes and the exact
inclusion rule. MLLM users receive only this split. Human users continue to
receive the complete manifest. The generator validates that all 3,000 unique
radiologist-assignment images are a subset of the MLLM benchmark.

`assignments/mllm_prompts.json` freezes the English Q1-Q4 wording, exact choice
order, output fields, and the Q3 choice matrix. MLLMs must answer sequentially:
their own Q1 region and Q2 lymph-node answers determine the choices and context
shown for Q3, matching the web interface. The specification also lists inputs
that must remain hidden to prevent label leakage.

The browser UI asks Q1 region, Q2 site, and an image-quality check. The old
ground-truth confirmation question was removed to avoid label leakage. Images are
served by opaque image IDs instead of GT-bearing filenames.

The UI also shows live cumulative Q1/Q2 accuracy curves based only on completed
answers saved for the current user.

## Blinded radiologist assignments

The frozen radiologist cohort contains the 10,565 images that sg2162 marked as
`No issue` for Q4. Generate five deterministic, blinded groups before registration:

```bash
python3 web/generate_radiologist_groups.py
```

The generated files are:

- `assignments/radiologist_groups.json`: immutable image membership, stratification metadata, and source provenance.
- `assignments/group_registry.json`: persistent group claims by specialist users.

Human users continue to receive the complete manifest. The original five
radiologists atomically claimed anonymous Groups A–E. Every original group contains
the same 500 Phase 1 images followed by 500 group-specific Phase 2 images, for
1,000 answers per radiologist. Phase 2 starts automatically when Phase 1 is
complete. The 3,000 unique sampled images use the frozen dataset, region, site,
and lymph-node quotas in the assignment generator. Phase 1/Phase 2 are neutral
UI labels; shared/exclusive status is not exposed. A returning user always
resumes the existing group.

## Additional radiologists: Phase 3

`python3 web/generate_radiologist_phase3.py` preserves Groups A–E and all of
their Phase 1/2 image IDs. It adds Groups F–T for new radiologists. Each new
radiologist first answers the same 500 shared Phase 1 images, then automatically
continues to a non-overlapping Phase 3 group. Groups F–S contain 500 Phase 3
images each; Group T contains the final 565. Together, the Phase 3 groups cover
all 7,565 No-issue images not already present in Phase 1 or Phase 2.
New registrations claim the first available group from F–T. Once all available
groups have been claimed, registration returns HTTP 409 with the
`no_available_group` code.

The UI does not reveal the stratification fields or show accuracy feedback to
radiologist users, preventing assignment and iterative ground-truth leakage.

Do not regenerate the frozen groups after radiologist registration begins. The
generator refuses to replace assignments while the registry contains claims.
