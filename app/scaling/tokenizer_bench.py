"""Check lightweight Rust tokenization matches Transformers without importing it at startup."""
import argparse
import json
from pathlib import Path
import time
from common import save, percentiles


def main(args):
    started=time.perf_counter()
    from tokenizers import Tokenizer
    tokenizer=Tokenizer.from_file(str(args.run/'text-encoder/tokenizer.json'))
    tokenizer.enable_truncation(max_length=64)
    tokenizer.enable_padding(length=64,pad_id=0,pad_token='<pad>')
    ready_ms=(time.perf_counter()-started)*1000
    original=json.loads((args.run/'query-tokens.json').read_text())
    timings=[]
    for query,expected in zip(original['queries'],original['input_ids']):
        started=time.perf_counter();actual=tokenizer.encode(query).ids
        timings.append((time.perf_counter()-started)*1000)
        assert actual==expected,'Tokenizer IDs differ for '+query
    # Import heavyweight reference only after the startup/latency measurement.
    from transformers import AutoTokenizer
    reference=AutoTokenizer.from_pretrained(args.run/'text-encoder',local_files_only=True)
    extra=['DUNĂRE ȘI ȚARĂ','  GOATS\n  and  dogs! ','👩‍🌾 capre','word '*200,'','a\u0306','hello—world']
    for query in extra:
        expected=reference(query,padding='max_length',max_length=64,truncation=True)['input_ids']
        assert tokenizer.encode(query).ids==expected,'Unicode/long-query tokenizer mismatch: '+repr(query[:30])
    result=dict(ready_ms=ready_ms,latency=percentiles(timings),saved_queries=len(original['queries']),
                extra_cases=len(extra),status='passed',mask='Production SigLIP model_input_names excludes attention_mask; encoder receives all ones.')
    save(args.run/'tokenizer.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    main(parser.parse_args())
