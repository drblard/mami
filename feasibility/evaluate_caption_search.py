"""Report ranks of user-labeled moments; unjudged candidates stay unjudged."""
import argparse
import json
from pathlib import Path


def evaluate(results, source, labels):
    groups = {g['query']: g['hits'] for g in source}
    aliases = {
        'milking goats': ('A video of the goat milking.', 'relevant_ranks'),
        'mulsul caprelor': ('A video of the goat milking.', 'relevant_ranks'),
        'feeding goats': ('A person feeds the goats.', 'relevant_ranks'),
        'picking plums': ('A family is picking plums.', 'relevant_to_plum_picking_ranks'),
        'culesul prunelor': ('A family is picking plums.', 'relevant_to_plum_picking_ranks'),
    }
    judgments = {q['query']: q for q in labels['queries']}
    report = []
    for row in results:
        if row['query'] not in aliases:
            continue
        source_query, field = aliases[row['query']]
        positive = {(groups[source_query][r - 1]['path'], groups[source_query][r - 1]['timestamp'])
                    for r in judgments[source_query][field]}
        entry = {'query': row['query'], 'source_positive_count': len(positive)}
        for method in ('visual', 'caption'):
            entry[method] = [{'rank': rank, 'path': hit['path'], 'timestamp': hit['timestamp']}
                             for rank, hit in enumerate(row[method], 1)
                             if (hit['path'], hit['timestamp']) in positive]
        report.append(entry)
    return {'caveat': 'Development-set positive ranks only, not precision or full-library recall. '
            'Labels apply to exact source moments. Other moments are unjudged; bringing-food '
            'queries do not inherit feeding labels. Strawberry activity is known absent, '
            'but both retrieval methods still return nearest neighbors.', 'queries': report}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--labels', default=str(Path(__file__).with_name('contextual-labels.json')))
    args = parser.parse_args()
    print(json.dumps(evaluate(*(json.loads(Path(p).read_text())
                               for p in (args.results, args.source, args.labels))), indent=2))
