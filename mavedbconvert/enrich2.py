import re
import os
from itertools import groupby
import logging
from operator import itemgetter

import hgvsp
from hgvsp import constants as hgvsp_constants
from tqdm import tqdm

import pandas as pd
import numpy as np
from pandas.testing import assert_index_equal

from . import LOGGER, constants, filters, utilities, validators, base


__all__ = [
    'Enrich2',
    'drop_null',
    'flatten_column_names',
    'get_count_dataframe_by_condition',
    'get_replicate_score_dataframes',
]


logger = logging.getLogger(LOGGER)


def apply_offset(variant, offset):
    variants = []
    for v in variant.split(','):
        if len(v.strip().split(' ')) == 2:
            nt, pro = v.strip().split(' ')
        elif v.strip()[0] == 'p':
            nt, pro = None, v.strip()
        else:
            nt, pro = v.strip(), None
        
        if nt is not None:
            import hgvsp
            nt = utilities.NucleotideSubstitutionEvent(nt)
            nt.position -= offset
            if nt.position < 1:
                raise ValueError(
                    "Position after offset {} "
                    "applied to {} is negative.".format(offset, nt.variant)
                )
            nt = nt.format
        if pro is not None:
            use_brackets = False
            if pro.startswith('(') and pro.endswith(')'):
                pro = pro[1:-1]
                use_brackets = True
            pro = utilities.ProteinSubstitutionEvent(pro)
            adjusted_offset = (1, -1)[offset < 0] * (abs(offset) // 3)
            pro.position -= adjusted_offset
            if pro.position < 1:
                raise ValueError(
                    "Position after offset {} "
                    "applied to {} is negative.".format(
                        adjusted_offset, nt.variant)
                )
            pro = pro.format
            if use_brackets:
                pro = '({})'.format(pro)

        variants.append('{} {}'.format(
            '' if nt is None else nt,
            '' if pro is None else pro
        ).strip())
        
    return ', '.join(variants)


def fix_silent_hgvs_syntax(variant):
    variants = []
    
    for v in variant.split(','):
        if len(v.strip().split(' ')) != 2:
            variants.append(v)
        
        nt, pro = v.strip().split(' ')
        
    return ', '.join(variants)


def drop_null(scores_df, counts_df=None):
    """
    Drops null rows and columns. If `counts_df` is not None, then they
    must contain the same index and same variants under the HGVS columns
    `hgvs_nt` and `hgvs_pro`.

    Modification is inplace when `counts_df` is `None`

    Parameters
    ----------
    scores_df : `pd.DataFrame`
        Scores dataframe the columns `hgvs_nt`, `hgvs_pro` and `score`
    counts_df : `pd.DataFrame`
        Counts dataframe containing the columns `hgvs_nt` and `hgvs_pro`

    Raises
    ------
    AssertionError
    ValueError

    Returns
    -------
    tuple[`pd.DataFrame`]
    """
    if counts_df is not None:
        # Apply filters to drop NaN columns and rows before validation
        # Join will discard rows in `counts_df` that have an index that
        # does not appear in `scores_df`. This shouldn't happen since we
        # validate that both indexes are the same.
        assert_index_equal(scores_df.index, counts_df.index)
        validators.validate_datasets_define_same_variants(scores_df, counts_df)
        joint_df = pd.concat(
            objs=[
                scores_df,
                counts_df[utilities.non_hgvs_columns(counts_df.columns)]
            ],
            axis=1, sort=False,
        )
        assert len(joint_df) == len(counts_df)
        filters.drop_na_columns(joint_df, inplace=True)
        filters.drop_na_rows(joint_df, inplace=True)

        score_columns = list(utilities.hgvs_columns(joint_df.columns)) + \
            list(utilities.non_hgvs_columns(scores_df.columns))
        score_columns = [c for c in score_columns if c in joint_df]
        scores_df = joint_df[score_columns]

        count_columns = list(utilities.hgvs_columns(joint_df.columns)) + \
            list(utilities.non_hgvs_columns(counts_df.columns))
        count_columns = [c for c in count_columns if c in joint_df]
        counts_df = joint_df[count_columns]

        assert_index_equal(scores_df.index, counts_df.index)
    else:
        filters.drop_na_columns(scores_df, inplace=True)
        filters.drop_na_rows(scores_df, inplace=True)

    return scores_df, counts_df


def flatten_column_names(columns, ordering):
    """
    Takes a column MultiIndex and joins each entry into a single underscore
    separated string based on the indices in the iterable `ordering`.

    Spaces in the MultiIndex entry are replaced with underscores.
    """
    values = [[x[y] for y in ordering] for x in columns]
    cnames = ['_'.join(x) for x in values]
    cnames = [x.replace(' ', '_') for x in cnames]
    return cnames


def get_replicate_score_dataframes(store, element=constants.variants_table):
    """
    Return a dictionary of DataFrames, one for each condition in store.
    Store is an open Enrich2 HDF5 file from an Experiment.
    Dictionary keys are condition names.
    """
    condition_dfs = dict()
    idx = pd.IndexSlice

    scores_key = '/main/{}/scores'.format(element)
    shared_key = '/main/{}/scores_shared'.format(element)
    if scores_key not in store:
        logger.warning(
            "Store is missing key {}. Skipping score file output.".format(
                scores_key))
        return condition_dfs
    if shared_key not in store:
        logger.warning(
            "Store is missing key {}. Skipping score file output.".format(
                shared_key))
        return condition_dfs

    for cnd in store['/main/{}/scores'.format(element)].columns.levels[0]:
        assert_index_equal(
            store['/main/{}/scores'.format(element)][cnd].index,
            store['/main/{}/scores_shared'.format(element)][cnd].index,
        )
        condition_dfs[cnd] = store['/main/{}/scores'.format(element)].\
            loc[:, idx[cnd, :, :]]
        condition_dfs[cnd].columns = condition_dfs[cnd].columns.levels[1]

        rep_scores = store['/main/{}/scores_shared'.format(element)].\
            loc[:, idx[cnd, :, :]]
        rep_scores.columns = flatten_column_names(rep_scores.columns, (2, 1))

        condition_dfs[cnd] = pd.merge(condition_dfs[cnd], rep_scores,
                                      how='inner', left_index=True,
                                      right_index=True)
    return condition_dfs


def get_count_dataframe_by_condition(store, cnd,
                                     element=constants.variants_table,
                                     filtered=None):
    """
    Return a DataFrame corresponding the condition cnd.

    Store is an open Enrich2 HDF5 file from an Experiment.

    filtered is a pandas Index containing variants to include. If it is none,
    the index of the DataFrame's score table for the element is used.
    """
    idx = pd.IndexSlice

    count_key = '/main/{}/counts'.format(element)
    if count_key not in store:
        logger.warning(
            "Store is missing key {}. Skipping count file output.".format(
                count_key))
        return None

    if filtered is None:
        scores_key = '/main/{}/scores'.format(element)
        if scores_key not in store:
            logger.warning(
                "Store is missing key {}. Skipping count file output.".format(
                    scores_key))
            return None
        filtered = store['/main/{}/scores'.format(element)].index

    df = store[count_key].loc[filtered, idx[cnd, :, :]]
    df.columns = flatten_column_names(df.columns, (1, 2))
    return df


class Enrich2(base.BaseProgram):
    __doc__ = base.BaseProgram.__doc__
    LOG_MSG = "Writing {elem} {df_type} for condition '{cnd}' to '{path}'."
    
    def convert(self):
        logger.info("Processing file {}".format(self.src))
        self.parse_input(self.load_input_file())

    def load_input_file(self):
        """
        Loads the input file specified at initialization into a dataframe.

        Returns
        -------
        `pd.HDFStore`
        """
        if not self.input_is_h5:
            raise TypeError(
                "Expected a HDF5 file. Found extension '{}'.".format(
                    self.extension))
        return pd.HDFStore(self.src, mode='r')

    def parse_row(self, row):
        """Delegates the correct method below."""
        if isinstance(row, (tuple, list)):
            variant, element = row
        else:
            variant, element = row, None
        variant = utilities.format_variant(variant)

        if variant in constants.special_variants:
            if element == constants.synonymous_table:
                return None, variant
            else:
                return variant, variant
        
        variant = fix_silent_hgvs_syntax(variant)
        variant = apply_offset(variant, self.offset)
        
        is_mixed = any([
            len(v.strip().split(' ')) == 2 for v in variant.split(',')
        ])
        is_nt_only = all([
            v.strip()[0] in hgvsp_constants.dna_prefix
            for v in variant.split(',')
        ])
        is_pro_only = all([
            v.strip()[0] in hgvsp_constants.protein_prefix
            for v in variant.split(',')
        ])

        if is_mixed:
            return self.parse_mixed_variant(variant, element)
        elif is_nt_only:
            return self.parse_nucleotide_variant(variant), None
        elif is_pro_only:
            return None, self.parse_protein_variant(variant)
        else:
            raise ValueError(
                "Could not infer type of HGVS string from '{}'.".format(
                    variant))

    def parse_input(self, store):
        """
        Convert all score and count data frames in the Enrich2 HDF5 file
        into MaveDB-ready `.csv` files.
        """
        synonymous_table = constants.synonymous_table
        variants_table = constants.variants_table
        has_syn = '/main/{}/scores'.format(synonymous_table) in store or \
                  '/main/{}/counts'.format(synonymous_table) in store
        has_var = '/main/{}/scores'.format(variants_table) in store or \
                  '/main/{}/counts'.format(variants_table) in store
        elements = []
        if has_syn:
            elements.append(synonymous_table)
        if has_var:
            elements.append(variants_table)

        for element in elements:
            rep_condition_dfs = get_replicate_score_dataframes(store, element)
            for cnd, score_df in rep_condition_dfs.items():
                count_df = get_count_dataframe_by_condition(
                    store, cnd, element, score_df.index
                )

                mave_scores_df = self.convert_h5_df(
                    df=score_df, element=element,
                    df_type=constants.score_type
                )
                mave_counts_df = None
                if count_df is not None:
                    assert_index_equal(score_df.index, count_df.index)
                    mave_counts_df = self.convert_h5_df(
                        df=count_df, element=element,
                        df_type=constants.count_type
                    )

                # This step checks both df define the same variants
                mave_scores_df, mave_counts_df = drop_null(
                    mave_scores_df, mave_counts_df
                )
                validators.validate_datasets_define_same_variants(
                    mave_scores_df, mave_counts_df
                )

                # If we have reached this point, all validators have passed.
                # Write to file if so.
                score_filepath = self.convert_h5_filepath(
                    basename=self.src_filename, element=element,
                    df_type=constants.score_type, cnd=cnd
                )
                mave_scores_df.to_csv(
                    score_filepath, sep=',', index=None, na_rep=np.NaN
                )

                if mave_counts_df is not None:
                    count_filepath = self.convert_h5_filepath(
                        basename=self.src_filename, element=element,
                        df_type=constants.count_type, cnd=cnd
                    )
                    mave_counts_df.to_csv(
                        count_filepath, sep=',', index=None, na_rep=np.NaN
                    )
        store.close()

    def convert_h5_filepath(self, basename, element, df_type, cnd):
        """
        Combine the destination, basename, condition name, and data frame type
        (expected to be "counts" or "scores") into a filepath for the output
        file.

        Returns a file path in the form:

            `<dst>/mavedb_<basename>_<counts|scores>_<element>_<condition>.csv`

        All spaces in the file name (but NOT the destination path name) are
        replaced by underscores.
        """
        filename = 'mavedb_{}_{}_{}_{}.csv'.format(
            basename, element, df_type, cnd)
        filename = re.sub(r'\s+', '_', filename)
        filepath = os.path.normpath(os.path.join(
            self.output_directory, filename))
        logger.info(
            self.LOG_MSG.format(
                elem=element, df_type='scores', cnd=cnd, path=filepath))
        return filepath

    def convert_h5_df(self, df, element, df_type):
        """
        Creates and outputs a mavedb data frame based on the data frame `df`
        that was extracted from an Enrich2 HDF5 file.
        """
        variants = tqdm(df.index, desc='Parsing variants', total=len(df.index))
        nt_protein_tups = [self.parse_row((v, element)) for v in variants]
        data = {
            constants.nt_variant_col: [tup[0] for tup in nt_protein_tups],
            constants.pro_variant_col: [tup[1] for tup in nt_protein_tups],
        }
        columns = list(constants.variant_columns)
        for column in df.columns:
            column_type = df.dtypes[column]
            column_values = df[column].values

            if column in constants.variant_columns:
                astype = str
            elif np.issubdtype(column_type, np.floating):
                astype = np.float
            elif np.issubdtype(column_type, np.signedinteger):
                astype = np.int
            else:
                logger.warning(
                    "Dropping non-numeric column '{}'".format(column))
                continue

            data[column] = utilities.format_column(column_values, astype)
            columns.append(column)

        mave_df = pd.DataFrame(data=data, columns=columns, index=df.index)
        logger.info("Running MaveDB compliance validation.")
        validators.validate_mavedb_compliance(mave_df, df_type)
        return mave_df

    def parse_mixed_variant(self, variant, element=None):
        """
        Parses a comma delimited string containing mixed HGVS syntax. Each
        variant is expcted to follow the format:
            c.<event> (p.<event>), c.<event> (p.<event>), ...
        """
        variant = utilities.format_variant(variant)
        if variant in constants.special_variants:
            if element == constants.synonymous_table:
                return None, variant
            else:
                return variant, variant
        else:
            mixed_variants = [p.strip().split(' ') for p in variant.split(',')]
            mixed_variants = [
                (utilities.format_variant(nt), utilities.format_variant(pro))
                for (nt, pro) in mixed_variants
            ]
            # Group variants by their codon position. This will shuffle
            # variant ordering compared to the input string.
            key = lambda x: utilities.\
                NucleotideSubstitutionEvent(x[0]).codon_position()
            codon_groups = groupby(sorted(mixed_variants, key=key), key=key)

            # Store a nucleotide variants index in the original string
            # to preserve order in the output variant.
            variant_index = {
                nt: i for i, (nt, pro) in enumerate(mixed_variants)}
            parsed_variants = {i: () for i in range(len(mixed_variants))}

            # For each codon group, if applicable, infer the correct
            # synonymous syntax.
            for _, codon_group in codon_groups:
                codon_group = list(codon_group)

                # Infer the correct synonymous syntax from the relevant
                # mutations within the codon.
                synonymous_events = [
                    (nt, pro) for (nt, pro) in codon_group if 'p.=' in pro]
                if synonymous_events and \
                        len(codon_group) != len(synonymous_events):
                    logger.warning(
                        "Codon group '{grp}' from variant '{var}' "
                        "is partially synonymous.".format(
                            grp=codon_group, var=variant))
                for nt, _ in synonymous_events:
                    inferred_pro = self.infer_silent_aa_substitution(
                        list(map(itemgetter(0), synonymous_events)), variant)
                    parsed_variants[variant_index[nt]] = (nt, inferred_pro)

                non_synonymous_events = [
                    (nt, pro) for (nt, pro) in codon_group if 'p.=' not in pro]
                for nt, pro in non_synonymous_events:
                    parsed_variants[variant_index[nt]] = (nt, pro)

            nt_variants = [t[0] for t in parsed_variants.values()]
            pro_variants = [t[1] for t in parsed_variants.values()]
            return self.parse_nucleotide_variant(nt_variants), \
                self.parse_protein_variant(pro_variants)

    def infer_silent_aa_substitution(self, codon_variants, variant=None):
        """
        Enrich2 outputs `p.=` for silent protein changes. This is incorrect.
        The correct format is `p.<aa><position>=`. Given an associated
        nucleotide substitution event, and the associated wildtype, this
        function infers the correct silent protein subsitution syntax.

        Parameters
        ----------
        codon_variants : Union[str, list]
            List of variants corresponding to a particular codon.

        variant : str, optional
            Original variant. Used when logging error messages.

        Returns
        -------
        str
        """
        if isinstance(codon_variants, str):
            codon_variants = [codon_variants, ]
        codon_variants = sorted(
            [utilities.NucleotideSubstitutionEvent(v) for v in codon_variants],
            key=lambda x: x.position
        )
        string_rep = ', '.join([str(v) for v in codon_variants])

        if len(set(c.codon_position() for c in codon_variants)) > 1:
            raise ValueError(
                "Codon group '{grp}' contains variants from "
                "different codons.".format(grp=string_rep)
            )

        # Enrich2 uses 1-based positions
        for v in codon_variants:
            if v.position > len(self.wt_sequence):
                raise IndexError(
                    "Error inferring corrected synonymous syntax. Coordinate "
                    "{pos} (1-based) is out of bounds in {hgvs}. The maximum "
                    "1-based index of the wild-type sequence "
                    "(offset {offset}) is {idx} (length {len_}).".format(
                        hgvs=str(v), pos=v.position, offset=self.offset,
                        idx=len(self.wt_sequence), len_=len(self.wt_sequence)))

            if v.silent:
                v.ref = self.wt_sequence[v.position - 1]
                v.alt = self.wt_sequence[v.position - 1]
            elif not v.silent and self.wt_sequence[v.position - 1] != v.ref:
                raise ValueError(
                    "Error inferring corrected synonymous syntax. "
                    "Base '{base}' at position {pos} (1-based) "
                    "in the wild-type sequence (offset {offset}) does not "
                    "match the base suggested by variant '{variant}' in "
                    "row '{row}'.".format(
                        pos=v.position, base=self.wt_sequence[v.position - 1],
                        offset=self.offset, variant=str(v), row=variant))

        # aa_pos is returned as 1-based.
        aa_pos = codon_variants[0].codon_position()
        wt_codon = self.codons[aa_pos - 1]
        mut_codon = wt_codon
        for v in codon_variants:
            within_frame_pos = v.codon_frame_position()
            mut_codon = mut_codon[:(within_frame_pos - 1)] + v.alt + \
                mut_codon[(within_frame_pos - 1) + 1:]

        wt_aa = constants.AA_CODES[constants.CODON_TABLE[wt_codon.upper()]]
        mut_aa = constants.AA_CODES[constants.CODON_TABLE[mut_codon.upper()]]
        if wt_aa != mut_aa:
            raise ValueError(
                "Error inferring corrected synonymous syntax. "
                "Wild-type codon ({}, {}) is not synonymous with "
                "the mutant codon ({}, {}) suggested by the codon group "
                "'{}'.".format(
                    wt_codon, wt_aa, mut_codon, mut_aa, string_rep))
        return 'p.{aa}{pos}='.format(aa=wt_aa, pos=aa_pos)

    @staticmethod
    def parse_protein_variant(variant):
        """
        Parses a comma delimited string containing only protein HGVS syntax.
        """
        if isinstance(variant, list):
            variants = [utilities.format_variant(v) for v in variant]
        else:
            variant = utilities.format_variant(variant)
            variants = [h.strip() for h in variant.split(',')]

        for i, variant in enumerate(variants):
            if constants.surrounding_brackets_re.fullmatch(variant):
                variant = variant[1:-1]
            variants[i] = utilities.format_variant(variant)

        if variants[0] in constants.special_variants:
            return variants[0]

        for variant in variants:
            if variant in constants.special_variants:
                continue
            if not hgvsp.protein.single_variant_re.fullmatch(variant):
                raise ValueError(
                    "'{variant}' contains invalid protein HGVS syntax.".format(
                        variant=variant)
                )
        variants = [v[2:] for v in variants]
        return utilities.hgvs_pro_from_event_list(variants)

    @staticmethod
    def parse_nucleotide_variant(variant):
        """
        Parses a comma delimited string containing only nucleotide HGVS syntax.
        """
        if isinstance(variant, list):
            variants = [utilities.format_variant(v) for v in variant]
        else:
            variant = utilities.format_variant(variant)
            variants = [v.strip() for v in variant.split(',')]

        if variants[0] in constants.special_variants:
            return variants[0]

        for v in variants:
            if v in constants.special_variants:
                continue
            if not hgvsp.dna.single_variant_re.fullmatch(v):
                raise ValueError(
                    "'{variant}' contains invalid DNA/RNA HGVS syntax.".format(
                        variant=v)
                )

        prefix = variants[0][0]  # First char of first variant.
        n_prefix_types = len(set(
            [v[0] for v in variants
             if v not in constants.special_variants]
        ))
        if n_prefix_types != 1:
            raise ValueError(
                "'{variant}' contains variants with multiple prefix "
                "types.".format(variant=variant)
            )
        variants = [v[2:] for v in variants]
        return utilities.hgvs_nt_from_event_list(variants, prefix=prefix)
