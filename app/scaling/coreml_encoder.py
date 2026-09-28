"""Export the verified text tower and measure Core ML parity on saved query tokens."""
import argparse
import json
import shutil
import time
from pathlib import Path
from common import lock, progress, save, percentiles


def main(args):
    with lock(args.run, 'coreml') as run:
        import numpy as np
        import torch
        import coremltools as ct
        from transformers import AutoTokenizer, Siglip2TextModel
        torch.set_num_threads(4)
        tokenizer = AutoTokenizer.from_pretrained(run / 'text-encoder', local_files_only=True)
        queries = json.loads((run / 'queries.json').read_text())
        tokens = tokenizer(queries, padding='max_length', max_length=64, truncation=True, return_tensors='pt')
        save(run / 'query-tokens.json', dict(queries=queries, input_ids=tokens['input_ids'].tolist(),
                                           attention_mask=tokens.get('attention_mask', torch.ones_like(tokens['input_ids'])).tolist()))
        name = 'SiglipTextFP32' if args.float32 else 'SiglipText'
        package = run / (name+'.mlpackage')
        if not package.exists():
            model = Siglip2TextModel.from_pretrained(run / 'text-encoder', local_files_only=True,
                                                    attn_implementation='eager').eval()
            class Encoder(torch.nn.Module):
                def __init__(self, model):
                    super().__init__()
                    self.model = model

                def forward(self, input_ids, attention_mask):
                    # Fixed-shape bidirectional padding mask avoids the generic
                    # Transformers mask builder's unsupported aten::new_ones.
                    hidden = self.model.embeddings(input_ids=input_ids)
                    mask = (1.0-attention_mask.to(hidden.dtype))[:, None, None, :] * -10000.0
                    hidden = self.model.encoder(inputs_embeds=hidden, attention_mask=mask).last_hidden_state
                    hidden = self.model.final_layer_norm(hidden)
                    features = self.model.head(hidden[:, -1, :])
                    return torch.nn.functional.normalize(features.float(), dim=-1)

            wrapped = Encoder(model).eval()
            example = (tokens['input_ids'][:1], tokens.get('attention_mask', torch.ones_like(tokens['input_ids']))[:1])
            progress(run, 'coreml', stage='trace')
            with torch.no_grad():
                parity = wrapped(*example).numpy()[0]
                if not np.allclose(parity, np.load(run / 'queries.npy')[0], atol=1e-5):
                    raise ValueError('Fixed-shape export wrapper changed query embeddings')
                traced = torch.jit.trace(wrapped, example, strict=False)
            progress(run, 'coreml', stage='convert')
            converted = ct.convert(traced, inputs=[ct.TensorType(name='input_ids', shape=example[0].shape, dtype=np.int32),
                                                  ct.TensorType(name='attention_mask', shape=example[1].shape, dtype=np.int32)],
                                   outputs=[ct.TensorType(name='embedding')], convert_to='mlprogram',
                                   minimum_deployment_target=ct.target.macOS14,
                                   compute_precision=ct.precision.FLOAT32 if args.float32 else ct.precision.FLOAT16,
                                   compute_units=ct.ComputeUnit.CPU_ONLY if args.float32 else ct.ComputeUnit.CPU_AND_NE)
            converted.save(str(package))
        progress(run, 'coreml', stage='load')
        started = time.perf_counter()
        converted = ct.models.MLModel(str(package), compute_units=ct.ComputeUnit.CPU_ONLY if args.float32 else ct.ComputeUnit.CPU_AND_NE)
        load_seconds = time.perf_counter()-started
        original = np.load(run / 'queries.npy')
        timings, cosine, outputs = [], [], []
        for i, query in enumerate(queries):
            inputs = {name: np.array(value[i:i+1],dtype=np.int32) for name,value in
                      json.loads((run / 'query-tokens.json').read_text()).items() if name != 'queries'}
            started = time.perf_counter()
            output = converted.predict(inputs)['embedding'].ravel().astype(np.float32)
            timings.append((time.perf_counter()-started)*1000)
            cosine.append(float(output @ original[i] / (np.linalg.norm(output)*np.linalg.norm(original[i]))))
            outputs.append(output)
        np.save(run / ('queries-coreml-fp32.npy' if args.float32 else 'queries-coreml.npy'), np.stack(outputs))
        compiled = run / (name+'.mlmodelc')
        if not compiled.exists():
            shutil.copytree(ct.models.utils.compile_model(str(package)), compiled)
        result = dict(load_seconds=load_seconds, latency=percentiles(timings), cosine_min=min(cosine),
                      first_ms=timings[0], package_bytes=sum(p.stat().st_size for p in package.rglob('*') if p.is_file()),
                      timings_ms=timings, cosine=cosine, compute_units='CPU_ONLY' if args.float32 else 'CPU_AND_NE')
        save(run / ('coreml-fp32.json' if args.float32 else 'coreml.json'), result)
        progress(run, 'coreml', stage='complete', **result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--float32', action='store_true')
    main(parser.parse_args())
