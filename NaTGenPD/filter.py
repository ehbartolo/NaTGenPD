# -*- coding: utf-8 -*-
"""
Data filtering utilities
@author: mrossol
"""
import concurrent.futures as cf
import logging
import numpy as np
import os
import pandas as pd

from NaTGenPD.cluster import SingleCluster, ClusterCC
from NaTGenPD.handler import CEMS, Fits

logger = logging.getLogger(__name__)


class Filter:
    """
    Run Cluster filters on all units
    """
    FILTERS = {'Boiler': SingleCluster.filter,
               'CT': SingleCluster.filter,
               'CC': ClusterCC.filter}

    def __init__(self, clean_h5, years=1):
        """
        Parameters
        ----------
        clean_h5 : str
            Path to .h5 file with pre-cleaned CEMS data
        years : int
            Number of years of data being filtered
        """
        self._clean_h5 = clean_h5
        self._years = years

    @property
    def total_points(self):
        """
        Number of total points possible in dataset

        Returns
        -------
        points : int
            total number of possible timesteps
        """
        points = self._years * 8760
        return points

    def filter_group(self, group_type, parallel=True, **kwargs):
        """
        Filter all units of given group_type

        Parameters
        ----------
        group_type : str
            Group type (generator type + fuel type) to filter
        parallel : bool
            For each group filter units in parallel

        Returns
        -------
        group_df : pd.DataFrame
            Updated group DataFrame with cluster labels post filtering
        """
        logger.info('Filtering all {} units'.format(group_type))
        total_points = self.total_points
        min_samples = int(total_points / 1000)
        logger.debug('\t- Using min_samples = {}'.format(min_samples))
        threshold = int(total_points / 100)
        logger.debug('\t- Skipping units with < {} points'.format(threshold))
        filter = self.FILTERS.get(group_type.split(' (')[0],
                                  SingleCluster.filter)

        with CEMS(self._clean_h5, mode='r') as f:
            group = f[group_type]

        if parallel:
            with cf.ProcessPoolExecutor() as executor:
                futures = []
                for unit_id, unit_df in group.unit_dfs:
                    logger.debug('- Filtering unit {}'.format(unit_id))
                    futures.append(executor.submit(filter, unit_df,
                                                   min_samples,
                                                   threshold=threshold,
                                                   **kwargs))

                group_df = [future.result() for future in futures]
        else:
            group_df = []
            for unit_id, unit_df in group.unit_dfs:
                logger.debug('- Filtering unit {}'.format(unit_id))
                group_df.append(filter(unit_df, min_samples,
                                       threshold=threshold, **kwargs))

        group_df = pd.concat(group_df).sort_values(['unit_id', 'time'])
        return group_df.reset_index(drop=True)

    def filter_groups(self, out_h5, group_types, parallel=True, **kwargs):
        """
        Filter given group_types from clean_h5 and save to out_h5

        Parameters
        ----------
        out_h5 : str
            Path to .h5 file into which filtered data should be saved
        group_types : list
            Group types to filter
        parallel : bool
            For each group filter units in parallel
        kwargs : dict
            Internal kwargs
        """
        with CEMS(out_h5, mode='a') as f_out:
            for g_type in group_types:
                f_out[g_type] = self.filter_group(g_type, parallel=parallel,
                                                  **kwargs)

    def filter_all(self, out_h5, parallel=True, **kwargs):
        """
        Filter all groups in clean_h5 and save to out_h5

        Parameters
        ----------
        out_h5 : str
            Path to .h5 file into which filtered data should be saved
        parallel : bool
            For each group filter units in parallel
        kwargs : dict
            Internal kwargs
        """
        with CEMS(self._clean_h5, mode='r') as f_in:
            group_types = f_in.dsets

        with CEMS(out_h5, mode='w') as f_out:
            for g_type in group_types:
                f_out[g_type] = self.filter_group(g_type, parallel=parallel,
                                                  **kwargs)

    @classmethod
    def run(cls, clean_h5, out_h5, group_types=None, years=1, parallel=True,
            **kwargs):
        """
        Filter all groups in clean_h5 and save to out_h5

        Parameters
        ----------
        clean_h5 : str
            Path to .h5 file with pre-cleaned CEMS data
        out_h5 : str
            Path to .h5 file into which filtered data should be saved
        group_types : list
            Group types to filter, if None, filter all
        years : int
            Number of years worth of data being filtered
        parallel : bool
            For each group filter units in parallel
        kwargs : dict
            Internal kwargs
        """
        f = cls(clean_h5, years=years)
        if group_types is not None:
            f.filter_groups(out_h5, group_types, parallel=parallel, **kwargs)
        else:
            f.filter_all(out_h5, parallel=parallel, **kwargs)


class PolyFit:
    """
    Fit filtered units to a polynomial
    """
    META_COLS = ['latitude', 'longitude', 'state', 'EPA_region', 'NERC_region',
                 'unit_type', 'fuel_type', 'group_type']

    def __init__(self, filtered_h5, order=4):
        """
        Parameters
        ----------
        filtered_h5 : str
            Path to .h5 file containing filtered CEMS data
        order : int
            Order of the polynomial fit
        """
        self._h5 = filtered_h5
        self._order = order

    @staticmethod
    def extract_fit(load, heat_rate, order=4, points=5):
        """
        Fit unit to a polynomial of given order

       Parameters
        ----------
        load : ndarray
            Load data for unit
        heat_rate : ndarray
            Heat Rate data for unit
        order : int
            Order/degree of the polynomial fit
        points : int
            Number of load/heat-rate points to save

        Returns
        -------
        unit_fit : pandas.Series
            Heat rate fit and meta-data for unit
            Fit parameters p = [p_n, p_n-1, ...]
            Such that y = p_n x^n + p_n-1 x^n-1 ...
        """
        fit_params = None
        load_range = None
        hr_range = None
        if load.size:
            load_min = load.min()
            load_max = load.max()
            try:
                fit = np.polyfit(load, heat_rate, order, full=True)
                if fit[2] > 1:
                    fit_params = fit[0]
                    load_range = np.linspace(load_min, load_max, points)
                    hr_range = np.poly1d(fit_params)(load_range)
            except Exception:
                logger.exception('Cannot fit unit')
        else:
            load_min = np.nan
            load_max = np.nan

        unit_fit = pd.Series(fit_params, index=['a4', 'a3', 'a2', 'a1', 'a0'])
        load_index = ['load_{:}'.format(i) for i in range(1, points + 1)]
        load_index[0] = 'load_min'
        load_index[-1] = 'load_max'

        unit_load = pd.Series(load_range, index=load_index)
        unit_load['load_min'] = load_min
        unit_load['load_max'] = load_max
        unit_load['total_load'] = load.sum()
        unit_load['min_gen_perc'] = load_min / load_max

        hr_index = ['heat_rate({:})'.format(load_i) for load_i in load_index]
        unit_hr = pd.Series(hr_range, index=hr_index)

        return pd.concat([unit_fit, unit_load, unit_hr])

    def fit_unit(self, unit_df, **kwargs):
        """
        Extract meta data and heat-rate fit(s) for given unit

        Parameters
        ----------
        unit_df : pandas.DataFrame
            DataFrame for unit to be fit
        kwargs : dict
            internal kwargs

        Returns
        -------
        unit_fit : pandas.DataFrame
            DataFrame of heat-rate fit(s) for given unit
        """
        unit_meta = unit_df.iloc[0]
        unit_id = unit_meta['unit_id']
        clusters = sorted([label for label in unit_df['cluster'].unique()
                           if label >= 0])

        if len(clusters) > 1:
            unit_fit = []
            for label in clusters:
                fit_id = '{}-{}'.format(unit_id, label)

                pos = unit_df['cluster'] == label
                cluster_df = unit_df.loc[pos, ['load', 'heat_rate']]
                cluster_df = cluster_df.sort_values(['load', 'heat_rate'])
                load = cluster_df['load'].values
                heat_rate = cluster_df['heat_rate'].values
                cluster_fit = self.extract_fit(load, heat_rate,
                                               order=self._order, **kwargs)
                cluster_fit.name = fit_id
                unit_fit.append(cluster_fit.to_frame().T)

            unit_fit = pd.concat(unit_fit)
        else:
            fit_id = '{}'.format(unit_id)
            pos = unit_df['cluster'] >= 0
            cluster_df = unit_df.loc[pos, ['load', 'heat_rate']]
            cluster_df = cluster_df.sort_values(['load', 'heat_rate'])
            load = cluster_df['load'].values
            heat_rate = cluster_df['heat_rate'].values
            cluster_fit = self.extract_fit(load, heat_rate,
                                           order=self._order, **kwargs)
            cluster_fit.name = fit_id
            unit_fit = cluster_fit.to_frame().T

        for col in self.META_COLS:
            unit_fit.loc[:, col] = unit_meta[col]

        return unit_fit

    def fit_group(self, group_type, out_file=None, **kwargs):
        """
        Extract polynomial fits for all units in given group

        Parameters
        ----------
        group_type : str
            Group type (generator type + fuel type) to filter
        out_file : str
            Path to file inwhich to save fit information (.json or .csv)
        kwargs : dict
            internal kwargs

        Returns
        -------
        group_fits : pandas.DataFrame
            DataFrame of fit information
        """
        logger.info('Fitting all {} units'.format(group_type))
        with CEMS(self._h5, mode='r') as f:
            group = f[group_type]

        group_fits = []
        for unit_id, unit_df in group.unit_dfs:
            logger.debug('- Fitting unit {}'.format(unit_id))
            group_fits.append(self.fit_unit(unit_df, **kwargs))

        group_fits = pd.concat(group_fits)
        group_fits.index.name = 'unit_id'
        if out_file:
            logger.debug('- Saving fits to {}'
                         .format(out_file))
            if out_file.endswith('.csv'):
                group_fits.to_csv(out_file)
            elif out_file.endswith('.json'):
                group_fits.to_json(out_file)
            else:
                raise ValueError('Invalid file type, cannot save to .{}'
                                 .format(os.path.splitext(out_file)[-1]))

        return group_fits

    def fit_all(self, out_dir, **kwargs):
        """
        Extract unit_fits for all units

        Parameters
        ----------
        out_dir : str
            Directory into which fit files (.csvs) should be saved
        kwargs: dict
            Internal kwargs
        """
        with CEMS(self._h5, mode='r') as f:
            group_types = f.dsets

        for g_type in group_types:
            out_path = "{}_fits.csv".format(g_type)
            out_path = os.path.join(out_dir, out_path)
            _ = self.fit_group(g_type, out_file=out_path, **kwargs)

    @classmethod
    def run(cls, filtered_h5, out_dir, order=4, **kwargs):
        """
        Extract unit_fits for all units

        Parameters
        ----------
        filtered_h5 : str
            Path to .h5 file containing filtered CEMS data
        out_dir : str
            Directory into which fit files (.csvs) should be saved
        order : int
            Order of the polynomial fit
        kwargs: dict
            Internal kwargs
        """
        fit = cls(filtered_h5, order=order)
        fit.fit_all(out_dir, **kwargs)


class FitFilter:
    """
    Filter Heat Rate fits
    """
    def __init__(self, fit_dir):
        """
        Parameters
        ----------
        fit_dir : str
            Path to directory containing heat rate fit files
        """
        self._hr_fits = Fits(fit_dir)

    @property
    def hr_fits(self):
        """
        Returns
        -------
        _hr_fits : Fits
            Instance of Fits handler class
        """
        return self._hr_fits

    @staticmethod
    def _get_hr_min(unit, points=100):
        """
        Extract minimum heat rate from unit fits

        Parameters
        ----------
        unit : pandas.Series
            Row containing the fit stats and parameters for a single unit

        Returns
        -------
        hr_min: float
            Minimum heat rate from heat rate curve fit
        """
        params = [i for i in unit.index if i.startswith('a')]
        fit_params = unit[params]
        if not fit_params.isnull().any():
            fit_params = fit_params.values
            poly_fit = np.poly1d(fit_params)
            load_min, load_max = unit[['load_min', 'load_max']].values
            x = np.linspace(load_min, load_max, points)
            hr_min = poly_fit(x).min()
            if hr_min < 4.5:
                logger.warning('\t- Minimun heat rate value is < 4.5!')
                hr_min = None
        else:
            hr_min = None

        return hr_min

    @staticmethod
    def _min_hr_filter(min_hr, stdev_multiplier=2, threshold=(None, None)):
        """
        Filter out the most and least efficient units based on multiples of the
        standard deviation from the mean

        Parameters
        ----------
        min_hr : pd.Series
            Series of minimum heat_rate values for all generators with a
            valid fit
        stdev_multiplier : float
            Multiple of the stdev from the mean to use as the filter thresholds
        threshold : tuple
            Pre-impossed limits for the filter threshold

        Returns
        -------
        failed_units : ndarray
            Array of units that failed the filter
        """
        min_hr_values = min_hr.values
        if threshold[0] is not None:
            min_hr_values = min_hr_values[min_hr_values > threshold[0]]

        if threshold[1] is not None:
            min_hr_values = min_hr_values[min_hr_values < threshold[1]]

        mean = min_hr_values.mean()
        stdev = min_hr_values.std()
        thresh = np.array([-stdev_multiplier, stdev_multiplier]) * stdev + mean

        if threshold[0] is not None:
            thresh[0] = threshold[0]

        if threshold[1] is not None:
            thresh[1] = threshold[1]

        pos = np.logical_or(min_hr.values < thresh[0],
                            min_hr.values > thresh[1])
        failed_units = min_hr[pos].index

        return failed_units

    @staticmethod
    def _filter(group_df, **kwargs):
        """
        Filter units of given group

        Parameters
        ----------
        group_df : pd.DataFrame
            DataFrame of fits for a given group types
        kwargs : dict
            Internal kwargs

        Returns
        -------
        group_df : pd.DataFrame
            Updated DataFrame of fits with filtered units fit values
            (load, heat_rate, and params) set to None
        """
        group_df = group_df.set_index('unit_id')
        min_hr = group_df.apply(FitFilter._get_hr_min, axis=1).dropna()

        failed_units = FitFilter._min_hr_filter(min_hr, **kwargs)
        filter_cols = [c for c in group_df.columns
                       if c.startswith(('a', 'heat_rate', 'load'))
                       and c not in ['load_min', 'load_max']]
        logger.debug('\t- {} units being filtered'.format(len(failed_units)))
        group_df.loc[failed_units, filter_cols] = None

        return group_df.reset_index()

    @staticmethod
    def _filer_CCs(cc_df, cut_off=9, **kwargs):
        """
        Filter CC units

        Parameters
        ----------
        cc_df : pd.DataFrame
            DataFrame of CC fits for each operating mode
        cut_off : float
            Threshold above which units should not be included in filter.
            These units did not report the steam generation and are thus
            appear overly in-efficient for a CC
        kwargs : dict
            Internal kwargs

        Returns
        -------
        cc_df : pd.DataFrame
            Updated DataFrame of CC fits with filtered units fit values
            (load, heat_rate, and params) set to None
        """
        cc_df['cc_id'] = cc_df['unit_id'].str.split('-').str[0]
        cc_df = cc_df.set_index('cc_id')
        cc_min_hr = cc_df.apply(FitFilter._get_hr_min, axis=1)
        cc_min_hr = cc_min_hr.dropna().to_frame().reset_index()
        cc_min_hr = cc_min_hr.groupby('cc_id').min()

        failed_units = FitFilter._min_hr_filter(cc_min_hr,
                                                threshold=(None, cut_off),
                                                **kwargs)
        filter_cols = [c for c in cc_df.columns
                       if c.startswith(('a', 'heat_rate', 'load'))
                       and c not in ['load_min', 'load_max']]
        logger.debug('\t- {} units being filtered'.format(len(failed_units)))
        cc_df.loc[failed_units, filter_cols] = None

        return cc_df.reset_index(drop=True)

    def filter(self, out_dir=None, min_units=100, **kwargs):
        """
        Filter all group types

        Parameters
        ----------
        out_dir : str | NoneType
            Output directory to save filtered fits to, if None update in-place
        min_units : int
            Minimum number of units with fits needed for filtering
        kwargs : dict
            internal kwargs
        """
        logger.info('Filtering out the most and least efficient heat '
                    'rate fits')
        out_fits = None
        if out_dir is not None:
            out_fits = Fits(out_dir)

        for g_type in self._hr_fits.group_types:
            group_df = self._hr_fits[g_type]
            fit_units = np.sum(~group_df['a0'].isnull())
            if fit_units > min_units:
                logger.info('- Filtering {}'.format(g_type))
                if 'CC' in g_type:
                    group_df = self._filer_CCs(group_df, **kwargs)
                else:
                    group_df = self._filter(group_df, **kwargs)

                if out_fits is not None:
                    out_fits[g_type] = group_df
                else:
                    self._hr_fits[g_type] = group_df
            else:
                logger.warning('- Skipping {} as it only has {} unique fits'
                               .format(g_type, fit_units))
                if out_fits is not None:
                    out_fits[g_type] = group_df

    @classmethod
    def run(cls, fit_dir, out_dir=None, **kwargs):
        """
        Filter all group types in fit_dir and save to disk

        Parameters
        ----------
        fit_dir : str
            Path to directory containing heat rate fit files
        out_dir : str | NoneType
            Output directory to save filtered fits to, if None update in-place
        """
        ff = cls(fit_dir)
        ff.filter(out_dir=out_dir, **kwargs)
