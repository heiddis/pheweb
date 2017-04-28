
'''
This script creates json files which can be used to render QQ plots.
'''

# TODO: share the list `variants` with Manhattan and top_{hits,loci}.
# TODO: make gc_lambda for maf strata, and show them if they're >1.1?
# TODO: copy changes from <https://github.com/statgen/encore/blob/master/plot-epacts-output/make_qq_json.py> and then copy js too.
#    - unbinned_variants
#    - get_conf_int()

from ..utils import conf, round_sig, approx_equal, get_phenolist
from ..file_utils import VariantFileReader, write_json
from .load_utils import get_maf, exception_printer, star_kwargs, get_num_procs

import collections
import os
import math
import datetime
import multiprocessing
import scipy.stats
from boltons.fileutils import mkdir_p


NEGLOG10_PVAL_BIN_SIZE = 0.05 # Use 0.05, 0.1, 0.15, etc
NEGLOG10_PVAL_BIN_DIGITS = 2 # Then round to this many digits
NUM_BINS = 1000

NUM_MAF_RANGES = 4


def gc_value_from_list(neglog10_pvals, quantile=0.5):
    # neglog10_pvals must be in decreasing order.
    assert all(neglog10_pvals[i] >= neglog10_pvals[i+1] for i in range(len(neglog10_pvals)-1))
    neglog10_pval = neglog10_pvals[int(len(neglog10_pvals) * quantile)]
    pval = 10 ** -neglog10_pval
    return gc_value(pval, quantile)
def gc_value(pval, quantile=0.5):
    # This should be equivalent to this R: `qchisq(p, df=1, lower.tail=F) / qchisq(.5, df=1, lower.tail=F)`
    return scipy.stats.chi2.ppf(1 - pval, 1) / scipy.stats.chi2.ppf(1 - quantile, 1)
assert approx_equal(gc_value(0.49), 1.047457) # I computed these using that R code.
assert approx_equal(gc_value(0.5), 1)
assert approx_equal(gc_value(0.50001), 0.9999533)
assert approx_equal(gc_value(0.6123), 0.5645607)


def compute_qq(neglog10_pvals):
    # neglog10_pvals must be in decreasing order.
    assert all(neglog10_pvals[i] >= neglog10_pvals[i+1] for i in range(len(neglog10_pvals)-1))

    if len(neglog10_pvals) == 0:
        return []

    max_exp_neglog10_pval = -math.log10(0.5 / len(neglog10_pvals))
    max_obs_neglog10_pval = neglog10_pvals[0]

    if max_obs_neglog10_pval == 0:
        print('WARNING: All pvalues are 1! How is that supposed to make a QQ plot?')
        return []

    occupied_bins = set()
    for i, obs_neglog10_pval in enumerate(neglog10_pvals):
        exp_neglog10_pval = -math.log10( (i+0.5) / len(neglog10_pvals))
        exp_bin = int(exp_neglog10_pval / max_exp_neglog10_pval * NUM_BINS)
        obs_bin = int(obs_neglog10_pval / max_obs_neglog10_pval * NUM_BINS)
        occupied_bins.add( (exp_bin,obs_bin) )

    qq = []
    for exp_bin, obs_bin in occupied_bins:
        assert 0 <= exp_bin <= NUM_BINS, exp_bin
        assert 0 <= obs_bin <= NUM_BINS, obs_bin
        qq.append((
            exp_bin / NUM_BINS * max_exp_neglog10_pval,
            obs_bin / NUM_BINS * max_obs_neglog10_pval
        ))
    return sorted(qq)


def make_qq_stratified(variants):
    variants = sorted(variants, key=lambda v: v.maf)

    def make_strata(idx):
        # Note: slice_indices[1] is the same as slice_indices[0] of the next slice.
        # But that's not a problem, because range() ignores the last index.
        slice_indices = (len(variants) * idx//NUM_MAF_RANGES,
                         len(variants) * (idx+1)//NUM_MAF_RANGES)
        neglog10_pvals = sorted((variants[i].neglog10_pval for i in range(*slice_indices)), reverse=True)
        return {
            'maf_range': (variants[slice_indices[0]].maf,
                          variants[slice_indices[1]-1].maf),
            'count': len(neglog10_pvals),
            'qq': compute_qq(neglog10_pvals),
        }

    return [make_strata(i) for i in range(NUM_MAF_RANGES)]

def make_qq_unstratified(variants, include_qq):
    neglog10_pvals = sorted((v.neglog10_pval for v in variants), reverse=True)
    rv = {}
    if include_qq:
        rv['qq'] = compute_qq(neglog10_pvals)
    rv['count'] = len(neglog10_pvals)
    rv['gc_lambda'] = {}
    for perc in ['0.5', '0.1', '0.01', '0.001']:
        gc = gc_value_from_list(neglog10_pvals, float(perc))
        if math.isnan(gc) or abs(gc) == math.inf:
            print('WARNING: got gc_value {!r}'.format(gc))
        else:
            rv['gc_lambda'][perc] = round_sig(gc, 5)
    return rv



Variant = collections.namedtuple('Variant', ['neglog10_pval', 'maf', 'v'])
def augment_variants(variants, pheno):
    for v in variants:
        if v['pval'] == 0:
            print("Warning: There's a variant with pval 0 in {!r}.  (Variant: {!r})".format(pheno['phenocode'], v))
            continue
        neglog10_pval = -math.log10(v['pval'])
        maf = get_maf(v, pheno)
        yield Variant(neglog10_pval=neglog10_pval, maf=maf, v=v)

@exception_printer
@star_kwargs
def make_json_file(src_filename, dest_filename, pheno):
    with VariantFileReader(src_filename) as variant_dicts:
        variants = list(augment_variants(variant_dicts, pheno))
    rv = {}
    if variants:
        if variants[0].maf is not None:
            rv['overall'] = make_qq_unstratified(variants, include_qq=False)
            rv['by_maf'] = make_qq_stratified(variants)
        else:
            rv['overall'] = make_qq_unstratified(variants, include_qq=True)
    write_json(filename=dest_filename, data=rv)
    print('{}\t{} -> {}'.format(datetime.datetime.now(), src_filename, dest_filename))


def get_conversions_to_do():
    for pheno in get_phenolist():
        src_filename = os.path.join(conf.data_dir, 'augmented_pheno', pheno['phenocode'])
        dest_filename = os.path.join(conf.data_dir, 'qq', '{}.json'.format(pheno['phenocode']))
        if not os.path.exists(dest_filename) or os.stat(dest_filename).st_mtime < os.stat(src_filename).st_mtime:
            yield {'src_filename':src_filename, 'dest_filename':dest_filename, 'pheno':pheno}

def run(argv):

    mkdir_p(conf.data_dir + '/qq')
    mkdir_p(conf.data_dir + '/tmp')

    conversions_to_do = list(get_conversions_to_do())
    print('number of phenos to process:', len(conversions_to_do))
    with multiprocessing.Pool(get_num_procs()) as p:
        p.map(make_json_file, conversions_to_do)
