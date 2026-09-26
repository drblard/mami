# Initial findings — 2026-09-25

## Library

498 media files: 246 HEVC/AAC videos and 252 JPEGs, totaling 35.8 GB decimal.
Videos total 6,753.6 seconds (1h 52m 34s). All videos have container creation
timestamps; all JPEGs have EXIF DateTimeOriginal. No metadata-probe failures.

## Visual baseline

SigLIP 2 Base via PyTorch/MPS processed all 498 files into 1,041 image embeddings
in 435.2 seconds with no per-frame failures. Ten-second video sampling plus
one embedding per photo. This elapsed time includes cached model initialization
and overlaps other experiment jobs; it is not an isolated benchmark.

This establishes throughput, not retrieval accuracy. Results need human-labeled
queries, positive matches, and negative examples. Transformers emits warnings
about unused generation BOS/EOS defaults; image and text embedding paths run,
but query quality remains to be evaluated.

## Speech and translation

Whisper large-v3-turbo transcribed three clips totaling 109.3 seconds in 9.0
seconds after model download. Full-library transcription launched separately.

Full Whisper large-v3 translated the same three audio clips in 14.0 seconds
after download. The English results contain concerning discrepancies relative
to the Romanian transcripts: one `Muzica` transcript became an apparent cooking
introduction. Neither output has yet been checked against the audio by a Romanian
speaker. Do not expand direct audio translation on the strength of throughput.

Next comparison: translate saved Romanian transcript segments as text, keeping
their timestamps. This can preserve transcription mistakes, but avoids a second
independent speech-recognition interpretation and makes auditing easier.

## Output locations on Mac

- Inventory: `~/mami-lab/runs/inventory-20260925T150632007122Z`
- Romanian trial: `~/mami-lab/runs/speech-20260925T151300400310Z`
- Visual index: `~/mami-lab/runs/visual-20260925T151405900113Z`
- Audio-to-English trial: `~/mami-lab/runs/speech-english-20260925T151649766641Z`

These findings do not establish action-recognition accuracy, Romanian word error
rate, or translation quality. Those require reference examples and review.

## User review of top-eight visual results

| Query | User assessment |
|---|---|
| milking goats | 4/8 correct |
| feeding goats | uncertain: perhaps 1/8; 2/8 show goats eating |
| picking strawberries | 0/8 |
| grilling food by a river | 6/8 |
| person talking to the camera | 8/8 |
| mulsul caprelor | 0/8 |
| culesul căpșunilor | 0/8, not close |
| grătar lângă Dunăre | 7/8; other result is home/fence/ostriches |

These are top-eight relevance observations, not recall measurements. The user
confirmed that strawberry picking is absent from this 13-day library. Both
strawberry queries are therefore negative controls: success means reporting no
convincing match, rather than trying to retrieve eight results. The Romanian
river query is narrower than its English counterpart, so that pair is not a
controlled language comparison. Goat-milking results show a clear language gap.
Next: English query normalization and multi-frame verification of candidates.

The verification trial checks four nearby frames for each of the original top
eight candidates. This is a false-positive rejection experiment, not a recall
improvement: it cannot recover matching clips excluded by initial retrieval.
Its verdicts are unvalidated model outputs, not ground-truth judgments.

Strawberry negative-control details supplied by the user: five returned results
show family picking plums, one shows the wife emptying old goat straw into a
pile, one is unclear and shows her back, and one is a close-up with eyes closed.
This establishes a useful positive query (`picking plums`) and a hard negative
pair (same harvesting action, different fruit). Correctness requires both the
action and the object; generic fruit-picking evidence must not confirm berries.

## 7B verification and query translation failures

Qwen2.5-VL-7B accepted 6/8 goat-milking candidates, 7/8 feeding candidates,
1/8 strawberry candidates, 3/8 grilling candidates, and 6/8 talking candidates.
Compared with user assessments, it both over-accepts hard negatives and loses
some known positives. It is not suitable as a trusted result gate in this setup.

The same model mistranslated Romanian queries: `mulsul caprelor` became
`sheep's pasture`; `culesul căpșunilor` became `picking plums`; `grătar lângă
Dunăre` became `grater near Danube`. Query translation from this model must not
be silently used for retrieval. The original Romanian queries must be retained.

Qwen3-VL-32B-Instruct 4-bit is being evaluated against the same 40 candidates
and identical verification prompt as the 7B baseline. Model weights total 19.6 GB.

## 32B results

40 candidates took 1,116.9 seconds (~18.6 minutes) for frame extraction and
inference, excluding model download/loading. Supported/uncertain/unsupported:

| Query | Supported | Uncertain | Unsupported |
|---|---:|---:|---:|
| milking goats | 3 | 2 | 3 |
| feeding goats | 4 | 0 | 4 |
| picking strawberries | 0 | 0 | 8 |
| grilling food by a river | 1 | 2 | 5 |
| person talking to the camera | 3 | 2 | 3 |

The known-absent strawberry candidates were all rejected. But the strict
visible-evidence prompt is misaligned with some user relevance judgments:
grilling close-ups are rejected when the river is out of frame, and still
portraits are rejected because speech cannot be proved. Do not deploy this as
a hard result gate. Distinguish exact visible evidence from related/event
context and uncertain evidence. Cross-asset context must be attributed, not
treated as proof that a specific object/action occurs in a frame.

32B query translations: `goat mulberries` (incorrect), `picking strawberries`,
`grill near Danube`, and `plum picking`. A dedicated Romanian-to-English
NLLB-200-3.3B trial is next; bigger VLMs alone have not solved translation.

Review: `/tmp/opencode/mami-review-20260925/verification-32b.html` on Linux.

## Dedicated translation trial

NLLB-200-3.3B correctly conveyed strawberry harvesting, plum harvesting, and
barbecue by the Danube, but translated `mulsul caprelor` as `Milk and cream of
bovine animals, fresh or chilled`. It is not safe as an unchecked replacement
for the original query. A follow-up with contextual Romanian sentences and
alternate phrasing was attempted, but SSH authentication was blocked by the
local key agent (`agent refused operation`). Check whether its job directory
exists before retrying the launch, since the first attempt timed out.

After SSH access was restored, the contextual NLLB trial completed in 9.3
seconds (cached weights, including loading). All seven outputs were sensible:

- `O femeie mulge capre.` → `A woman milks goats.`
- `mulgerea caprelor` → `Milking of goats`
- `Un videoclip cu mulsul caprelor.` → `A video of the goat milking.`
- `Caut videoclipuri cu mulsul caprelor.` → `I'm looking for videos of goat milking.`
- Feeding goats, picking plums, and picking strawberries sentence forms also
  preserved the intended activity/object.

This supports testing contextual wrapping for short Romanian queries. It does
not establish general translation accuracy or justify silently replacing the
original query. The follow-up retrieval run uses the resulting English phrases
against the unchanged visual index, isolating query phrasing from model changes.

## Query-blind caption diagnostic

16 retrieved moments were described by Qwen3-VL-32B with no search query in its
prompt, in 388.6 seconds including initialization. This is a development sample.

- All three reviewed positive milking moments (contextual ranks 2, 3, 4) were
  explicitly described as milking. The sampled negative rank 1 was not described
  as milking. This is not an estimate of full-library precision or recall.
- Several plum-picking scenes were explicitly described as harvesting plums;
  other fruit scenes received generic fruit/orchard descriptions.
- No description mentioned strawberries. This alone does not prove a downstream
  semantic search will reject strawberry queries; that must be tested separately.
- Feeding remains problematic: feeding rank 2 (user-negative) was confidently
  described as feeding from a bucket; rank 4 (user-positive) received an uncertain
  feeding/adjusting-trough description. Caption-based retrieval still needs work.
- Some descriptions infer tools, animal species, or intentions. Do not treat
  model descriptions as authoritative metadata or automatically confirmed tags.

Recommendation: pursue caption + visual + speech retrieval, with uncertainty
preserved, rather than a hard verifier filter. Before indexing the whole library,
test actual caption retrieval and separate holdout examples. Full-library dense
captioning likely takes hours; measure photo/video costs separately and add
resumable checkpoints before launching it. Do not extrapolate a success rate
from the 16 examples used during development.

Linux review: `/tmp/opencode/mami-review-20260925/blind-captions-32b.html`.

## Revised activity: bringing food to goats

The user suggested carrying some form of goat food to goats as a more useful
query than feeding. Treat it as a distinct intent: evidence of transporting
feed toward goats/their feeding area, without requiring a visible handoff.
Carrying an unidentified bucket alone does not establish food (it may contain
milk, water, waste, or nothing). Clip context may establish contents/destination.
Existing feeding relevance labels must not be reused for this new activity.
A two-phrase visual baseline trial is running before more heavy inference.

## Caption retrieval diagnostic — completed

Run: `caption-search-20260925T190548716476Z`, using the original 16
query-blind descriptions and pinned multilingual E5 Base. Compared with SigLIP
on identical candidate moments (single-frame visual vs multi-frame captions).
Nine English/Romanian queries; elapsed 79.2 seconds includes initial model
download, so this is not a search-latency measurement.

Ranks of exact user-labeled positives, visual → caption:

| Query | Visual ranks | Caption ranks |
| --- | --- | --- |
| milking goats | 1, 2, 5 | 1, 2, 4 |
| feeding goats | 4 | 7 |
| picking plums (four labeled moments present) | 1, 2, 3, 4 | 1, 2, 3, 4 |
| mulsul caprelor | 4, 6, 10 | 9, 10, 13 |
| culesul prunelor (four labeled moments present) | 1, 2, 4, 7 | 1, 2, 3, 5 |

This does not establish precision/recall: other candidate moments are unjudged,
and the pool is a selected development set. Feeding labels are not transferred
to bringing-food queries. Both methods still return matches for absent
strawberry picking. The known incorrect feeding description ranks first for
feeding and bringing-food queries: retrieval can amplify caption errors.

Conclusion: caption-only retrieval is not a reliable replacement. English
milking moves slightly, feeding gets worse, and direct Romanian goat-milking
retrieval fails badly. Preserve visual retrieval while testing temporal evidence
and fast result inspection; do not expand this caption-only recipe library-wide.

Local results, HTML, and exact-moment evaluation:
`/tmp/opencode/mami-review-20260925/caption-retrieval-190548/`.

## One-second visual index details

All 498 files processed in 2,648.8 seconds (~44 minutes), initially yielding
7,113 samples with 15 failed end-of-clip seeks. Container duration can exceed
video duration, and a final timestamp can fall after the last frame. Clamping
to before the last frame boundary recovered all 15 in a separate index without
overwriting prior outputs. Final count: 7,128 samples, no unresolved failures.

Five sampling tests pass, including the audio-longer-than-video case. Frame
extraction errors now include FFmpeg stderr rather than only its exit code.

Seven unchanged queries were rerun using the same pinned SigLIP model. Search
took 3.8 seconds including model initialization and HTML generation (not an
isolated query-latency measurement). Side-by-side review on Linux:
`/tmp/opencode/mami-review-20260925/one-second-comparison.html`.
No retrieval quality improvement is claimed until reviewed.

### User review across both sides (16 displayed results per query)

- Bringing food to goats: 2/16 relevant.
- Carrying animal feed toward goats: 3/16.
- Milking goats: 7/16.
- Picking plums: 16/16 when related plum-tree photos count.
- Picking strawberries: 0/16; this activity is known absent, so all displayed
  results are false positives rather than missed positives.
- Grilling food by river: 13/16.
- Person talking to camera: 16/16.

These totals combine the 10-second and 1-second columns and may include the
same file twice. They do not establish a per-index precision improvement or
decline. User relevance includes related scene/event images, not only direct
proof of the literal action. Dense visual embeddings alone remain inadequate
for the requested action search. A production UI does not remedy that gap.

## Contextual-search user review

- Goat milking: ranks 2, 3, 4 relevant (3/8 versus initial 4/8).
- Feeding goats: rank 4 relevant (1/8).
- Plum picking: all eight are from the plum-picking activity. Ranks 1, 3, 4,
  5, 8 show multiple people. `Family` was an unnecessary experimental constraint;
  use `picking plums` and do not infer family relationships from appearance.
- Strawberry picking: none relevant; ranks 1, 4, 5, 6, 7, 8 are from plum picking.

Contextual translation has not improved action retrieval. Do not keep tuning
phrases against these examples. Next diagnostic: query-blind descriptions of
the actual retrieved moments, testing whether activity/object evidence can be
extracted before retrieval. This is a development set, not an independent test.
