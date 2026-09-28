"""Extract the pinned SigLIP text tower and verify its queries against the full model."""
import argparse
import json
import os
from pathlib import Path
import time
from common import lock, progress, save

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['OMP_NUM_THREADS'] = '4'

QUERIES = ['bringing food to goats', 'milking goats', 'picking plums', 'children playing outside',
           'a dog running', 'walking in the forest', 'cooking in a kitchen', 'a person talking to the camera',
           'a tractor in a field', 'sunset over water', 'snow on mountains', 'a birthday cake',
           'flowers in a garden', 'feeding chickens', 'a baby sleeping', 'driving a car',
           'a beach with people', 'a horse', 'a family eating together', 'cutting vegetables',
           'a woman holding a camera', 'a cow grazing', 'a river', 'a close-up of fruit']


def main(args):
    with lock(args.run, 'encoder') as run:
        started = time.perf_counter()
        import numpy as np
        import torch
        from transformers import AutoModel, AutoProcessor
        from huggingface_hub import snapshot_download
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        imports = time.perf_counter()-started
        path = snapshot_download('google/siglip2-base-patch16-224', revision='75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2',
                                 local_files_only=True, cache_dir=Path.home()/'mami-lab/cache/huggingface/hub',
                                 allow_patterns=['*.json','*.safetensors','*.model','*.txt','*.jinja'])
        processor = AutoProcessor.from_pretrained(path, local_files_only=True)
        model = AutoModel.from_pretrained(path, local_files_only=True).eval()
        text = model.text_model
        text_directory = run / 'text-encoder'
        if not text_directory.exists():
            text.save_pretrained(text_directory)
            processor.tokenizer.save_pretrained(text_directory)
        outputs, timings, errors = [], [], []
        with torch.inference_mode():
            for query in QUERIES:
                inputs = processor(text=[query], padding='max_length', truncation=True, return_tensors='pt')
                expected = model.get_text_features(**inputs).pooler_output
                start = time.perf_counter()
                actual = text(**inputs).pooler_output
                timings.append((time.perf_counter()-start)*1000)
                errors.append(float((expected-actual).abs().max()))
                outputs.append(torch.nn.functional.normalize(actual.float(),dim=-1).numpy()[0])
        np.save(run / 'queries.npy', np.stack(outputs))
        save(run / 'queries.json', QUERIES)
        result = dict(import_seconds=imports, text_CPU_ms=timings, max_absolute_error=max(errors),
                      text_parameters=sum(p.numel() for p in text.parameters()),
                      full_parameters=sum(p.numel() for p in model.parameters()))
        save(run / 'encoder.json', result)
        progress(run, 'encoder', stage='text_tower_verified', **result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    main(parser.parse_args())
