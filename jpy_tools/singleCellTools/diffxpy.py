from logging import log
import pandas as pd
import numpy as np
import scanpy as sc
import matplotlib.pyplot as plt
import seaborn as sns
import anndata
from scipy.stats import spearmanr, pearsonr, zscore
from loguru import logger
from io import StringIO
from concurrent.futures import ProcessPoolExecutor as Mtp
from concurrent.futures import ThreadPoolExecutor as MtT
import sh
import h5py
from tqdm import tqdm
from typing import (
    Dict,
    List,
    Optional,
    Union,
    Sequence,
    Literal,
    Any,
    Tuple,
    Iterator,
    Mapping,
    Callable,
)
import collections
from xarray import corr
from . import basic

def parseAdToDiffxpyFormat(
    adata: anndata.AnnData,
    testLabel: str,
    testComp: str,
    otherComp: Optional[Union[str, List[str]]] = None,
    batchLabel: Optional[str] = None,
    minCellCounts: int = 5,
    keyAdded: str = "temp",
    subset = True,
):
    if not otherComp:
        otherComp = list(adata.obs[testLabel].unique())
        otherComp = [x for x in otherComp if x != testComp]
    if isinstance(otherComp, str):
        otherComp = [otherComp]
    if subset:
        adata = adata[adata.obs[testLabel].isin([testComp, *otherComp])]
        sc.pp.filter_genes(adata, min_cells=minCellCounts)
    else:
        adata.obs['keep'] = adata.obs[testLabel].isin([testComp, *otherComp])
    adata.obs = adata.obs.assign(
        **{keyAdded: np.select([adata.obs[testLabel] == testComp], ["1"], "0")}
    )
    if batchLabel:
        adata.obs = adata.obs.assign(
            **{
                f"{batchLabel}_{keyAdded}": adata.obs[batchLabel].astype(str)
                + "_"
                + adata.obs[keyAdded].astype(str)
            }
        )
    return adata

def testTwoSample(
    adata: anndata.AnnData,
    keyTest: str = "temp",
    batchLabel: Optional[str] = None,
    quickScale: bool = False,
    sizeFactor: Optional[str] = None,
    constrainModel: bool = False,
) -> pd.DataFrame:
    """
    Use wald test between two sample.
    This function is always following `parseAdToDiffxpyFormat`

    Parameters
    ----------
    adata : anndata.AnnData
        generated by `parseAdToDiffxpyFormat`
    keyTest : str, optional
        `keyAdded` parameter of `parseAdToDiffxpyFormat`, by default 'temp'
    batchLabel : Optional[str], optional
        by default None
    quickScale : bool, optional
        by default False
    sizeFactor : Optional[str], optional
        by default None

    Returns
    -------
    pd.DataFrame
    """
    import diffxpy.api as de

    assert len(adata.obs[keyTest].unique()) == 2, "More Than Two Samples found"

    if batchLabel:
        if constrainModel:
            test = de.test.wald(
                data=adata,
                formula_loc=f"~ 1 + {keyTest} + {batchLabel}_{keyTest}",
                factor_loc_totest=keyTest,
                quick_scale=quickScale,
                size_factors=sizeFactor,
            )
        else:
            test = de.test.wald(
                data=adata,
                formula_loc=f"~ 1 + {keyTest} + {batchLabel}:{keyTest}",
                factor_loc_totest=keyTest,
                quick_scale=quickScale,
                size_factors=sizeFactor,
            )
    else:
        test = de.test.wald(
            data=adata,
            formula_loc=f"~ 1 + {keyTest}",
            factor_loc_totest=keyTest,
            quick_scale=quickScale,
            size_factors=sizeFactor,
        )
    return test.summary()


def vsRest(
    adata: anndata.AnnData,
    layer: Optional[str],
    testLabel: str,
    groups: Optional[List[str]] = None,
    batchLabel: Optional[str] = None,
    minCellCounts: int = 5,
    sizeFactor: Optional[str] = None,
    inputIsLog: bool = False,
    keyAdded: str = None,
    quickScale: bool = True,
    constrainModel: bool = False,
    threads: int = 1,
    copy: bool = False,
) -> Optional[Dict[str, pd.DataFrame]]:
    """
    use wald to find DEG

    Parameters
    ----------
    adata : anndata.AnnData
    layer : Optional[str]
        must be raw count data. if is not log-transformed, the `inputIsLog` must be assigned by `False`
    testLabel : str
        column name in adata.obs. used as grouping Infomation
    groups : Optional[List[str]], optional
        only use these groups, by default None
    batchLabel : Optional[str], optional
        column name in adata.obs. used as batch Infomation. by default None
    minCellCounts : int, optional
        used to filter genes. by default 5
    sizeFactor : Optional[str], optional
        column name in adata.obs. used as size factor Infomation. by default None
    inputIsLog : bool, optional
        is determined by `layer`. by default True
    keyAdded : str, optional
        key used to update adata.uns, by default None
    quickScale : bool, optional
        by default True
    copy : bool, optional
        by default False

    Returns
    -------
    Optional[Dict[str, pd.DataFrame]]
        return pd.DataFrame if copy
    """
    import scipy.sparse as ss
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing.managers import SharedMemoryManager
    from multiprocessing.shared_memory import SharedMemory
    
    layer = "X" if not layer else layer
    if not groups:
        groups = list(adata.obs[testLabel].unique())
    if not keyAdded:
        keyAdded = f"diffxpyVsRest_{testLabel}"
    ls_useCol = [testLabel]
    if batchLabel:
        ls_useCol.append(batchLabel)
    if sizeFactor:
        ls_useCol.append(batchLabel)
    adataOrg = adata.copy() if copy else adata
    adata = basic.getPartialLayersAdata(adataOrg, layer, ls_useCol)
    # adata.X = adata.X.A if ss.issparse(adata.X) else adata.X
    if inputIsLog:
        adata.X = ss.linalg.expm(adata.X) - 1 if ss.issparse(adata.X) else np.exp(adata.X) - 1
    adata = adata[adata.obs[testLabel].isin(groups)].copy()

    logger.info("start performing test")
    adataOrg.uns[keyAdded] = {"__cat": "vsRest"}

    if threads > 1:
        dt_diffxpyResult = {}
        mtx = adata.X.A if ss.issparse(adata.X) else adata.X
        adata.X = ss.csr_matrix(adata.shape)
        with SharedMemoryManager() as smm:
            shm = smm.SharedMemory(mtx.nbytes)
            shape = mtx.shape
            dtype = mtx.dtype
            mtxInShm = np.ndarray(shape=shape, dtype=dtype, buffer=shm.buf)
            np.copyto(mtxInShm, mtx)
            with ProcessPoolExecutor(threads) as mtp:
                for singleGroup in groups:
                    dt_diffxpyResult[singleGroup] = mtp.submit(
                        _getDiffxpy,
                        adata,
                        testLabel,
                        singleGroup,
                        batchLabel=batchLabel,
                        minCellCounts=minCellCounts,
                        quickScale=quickScale,
                        sizeFactor=sizeFactor,
                        constrainModel=constrainModel,
                        ls_shm = [shm.name, shape, dtype]
                    )
        logger.info(f"get marker done")
        dt_diffxpyResult = {x: y.result() for x, y in dt_diffxpyResult.items()}
        adataOrg.uns[keyAdded].update(dt_diffxpyResult)
    else:
        adata.X = adata.X.A if ss.issparse(adata.X) else adata.X
        for singleGroup in groups:
            ad_test = parseAdToDiffxpyFormat(
                adata,
                testLabel,
                singleGroup,
                batchLabel=batchLabel,
                minCellCounts=minCellCounts,
                keyAdded="temp",
            )
            test = testTwoSample(
                ad_test,
                "temp",
                batchLabel,
                quickScale,
                sizeFactor,
                constrainModel=constrainModel,
            )
            adataOrg.uns[keyAdded][singleGroup] = test
            logger.info(f"{singleGroup} done")
    if copy:
        return adataOrg.uns[keyAdded]

def pairWise(
    adata: anndata.AnnData,
    layer: Optional[str],
    testLabel: str,
    groups: Optional[List[str]] = None,
    batchLabel: Optional[str] = None,
    minCellCounts: int = 5,
    sizeFactor: Optional[str] = None,
    inputIsLog: bool = False,
    keyAdded: str = None,
    quickScale: bool = True,
    constrainModel: bool = False,
    copy: bool = False,
) -> Optional[Dict[str, pd.DataFrame]]:
    """
    use wald to find DEG

    Parameters
    ----------
    adata : anndata.AnnData
    layer : Optional[str]
        if is not log-transformed, the `inpuIsLog` must be assigned by `False`
    testLabel : str
        column name in adata.obs. used as grouping Infomation
    groups : Optional[List[str]], optional
        only use these groups, by default None
    batchLabel : Optional[str], optional
        column name in adata.obs. used as batch Infomation. by default None
    minCellCounts : int, optional
        used to filter genes. by default 5
    sizeFactor : Optional[str], optional
        column name in adata.obs. used as size factor Infomation. by default None
    inputIsLog : bool, optional
        is determined by `layer`. by default True
    keyAdded : str, optional
        key used to update adata.uns, by default None
    quickScale : bool, optional
        by default True
    copy : bool, optional
        by default False

    Returns
    -------
    Optional[Dict[str, pd.DataFrame]]
        return pd.DataFrame if copy
    """
    from itertools import product
    import scipy.sparse as ss

    layer = "X" if not layer else layer
    if not groups:
        groups = list(adata.obs[testLabel].unique())
    if not keyAdded:
        keyAdded = f"diffxpyPairWise_{testLabel}"
    ls_useCol = [testLabel]
    if batchLabel:
        ls_useCol.append(batchLabel)
    if sizeFactor:
        ls_useCol.append(batchLabel)
    adataOrg = adata.copy() if copy else adata
    adata = basic.getPartialLayersAdata(adataOrg, layer, ls_useCol)
    adata.X = adata.X.A if ss.issparse(adata.X) else adata.X
    if inputIsLog:
        adata.X = np.exp(adata.X) - 1
    adata = adata[adata.obs[testLabel].isin(groups)].copy()

    logger.info("start performing test")
    adataOrg.uns[keyAdded] = {"__cat": "pairWise"}
    for x, y in product(range(len(groups)), range(len(groups))):
        if (
            x >= y
        ):  # only calculate half combination of groups, then use these result to fullfill another half
            continue
        testGroup = groups[x]
        backgroundGroup = groups[y]
        ad_test = parseAdToDiffxpyFormat(
            adata,
            testLabel,
            testGroup,
            backgroundGroup,
            batchLabel=batchLabel,
            minCellCounts=minCellCounts,
            keyAdded="temp",
        )
        test = testTwoSample(
            ad_test,
            "temp",
            batchLabel,
            quickScale,
            sizeFactor,
            constrainModel=constrainModel,
        )
        adataOrg.uns[keyAdded][f"test_{testGroup}_bg_{backgroundGroup}"] = test
        logger.info(f"{testGroup} vs {backgroundGroup} done")
    for x, y in product(range(len(groups)), range(len(groups))):
        if x <= y:  # use these result to fullfill another half
            continue
        testGroup = groups[x]
        backgroundGroup = groups[y]
        adataOrg.uns[keyAdded][
            f"test_{testGroup}_bg_{backgroundGroup}"
        ] = adataOrg.uns[keyAdded][f"test_{backgroundGroup}_bg_{testGroup}"].copy()
        adataOrg.uns[keyAdded][f"test_{testGroup}_bg_{backgroundGroup}"][
            "log2fc"
        ] = -adataOrg.uns[keyAdded][f"test_{testGroup}_bg_{backgroundGroup}"][
            "log2fc"
        ]

    if copy:
        return adataOrg.uns[keyAdded]


def getMarker(
    adata: anndata.AnnData,
    key: str,
    qvalue=0.05,
    log2fc=np.log2(1.5),
    mean=0.5,
    detectedCounts=-1,
) -> pd.DataFrame:
    """
    parse `vsRest` and `pairWise` results

    Parameters
    ----------
    adata : anndata.AnnData
        after appy `vsRest` or `pairWise`
    key : str
        `keyAdded` parameter of `vsRest` or `pairWise`
    qvalue : float, optional
        cutoff, by default 0.05
    log2fc : [type], optional
        cutoff, by default np.log2(1.5)
    mean : float, optional
        cutoff, by default 0.5
    detectedCounts : int, optional
        cutoff, only usefull for `pairWise`, by default -1

    Returns
    -------
    pd.DataFrame
        [description]
    """

    def __twoSample(df_marker, qvalue=0.05, log2fc=np.log2(1.5), mean=0.5):
        df_marker = df_marker.query(
            f"qval < {qvalue} & log2fc > {log2fc} & mean > {mean}"
        ).sort_values("coef_mle", ascending=False)
        return df_marker

    def __vsRest(
        dt_marker: Dict[str, pd.DataFrame],
        qvalue,
        log2fc,
        mean,
        detectedCounts,
    ) -> pd.DataFrame:
        ls_markerParsed = []
        for comp, df_marker in dt_marker.items():
            if comp == "__cat":
                continue
            if "clusterName" not in df_marker.columns:
                df_marker.insert(0, "clusterName", comp)
            df_marker = __twoSample(df_marker, qvalue, log2fc, mean)
            ls_markerParsed.append(df_marker)
        return pd.concat(ls_markerParsed)

    def __pairWise(
        dt_marker: Dict[str, pd.DataFrame],
        qvalue,
        log2fc,
        mean,
        detectedCounts=-1,
    ) -> pd.DataFrame:
        import re

        ls_markerParsed = []
        ls_compName = []
        for comp, df_marker in dt_marker.items():
            if comp == "__cat":
                continue
            testedCluster = re.findall(r"test_([\w\W]+)_bg", comp)[0]
            bgCluster = re.findall(r"_bg_([\w\W]+)", comp)[0]
            ls_compName.append(bgCluster)
            if "testedCluster" not in df_marker.columns:
                df_marker.insert(0, "testedCluster", testedCluster)
            if "bgCluster" not in df_marker.columns:
                df_marker.insert(1, "bgCluster", bgCluster)

            df_marker = __twoSample(df_marker, qvalue, log2fc, mean)
            ls_markerParsed.append(df_marker)
        df_markerMerged = pd.concat(ls_markerParsed)
        df_markerMerged = (
            df_markerMerged.groupby(["testedCluster", "gene"])
            .agg(
                {
                    "gene": "count",
                    "bgCluster": lambda x: list(x),
                    "qval": lambda x: list(x),
                    "log2fc": lambda x: list(x),
                    "mean": lambda x: list(x),
                    "coef_mle": lambda x: list(x),
                }
            )
            .rename(columns={"gene": "counts"})
            .reset_index()
        )
        if detectedCounts <= 0:
            detectedCounts = len(set(ls_compName)) + detectedCounts
            logger.info(f"`detectedCounts` is parsed to {detectedCounts}")

        return df_markerMerged.query(f"counts >= {detectedCounts}")

    dt_marker = adata.uns[key]
    cate = dt_marker["__cat"]
    fc_parse = {
        "vsRest": __vsRest,
        "pairWise": __pairWise,
    }[cate]
    return fc_parse(dt_marker, qvalue, log2fc, mean, detectedCounts)


def _getDiffxpy(
    adata,
    testLabel,
    testComp,
    batchLabel,
    minCellCounts,
    quickScale,
    sizeFactor,
    constrainModel,
    ls_shm
):  
    import scipy.sparse as ss
    from multiprocessing.shared_memory import SharedMemory
    ad_parsed = parseAdToDiffxpyFormat(
        adata,
        testLabel,
        testComp,
        batchLabel=batchLabel,
        minCellCounts=minCellCounts,
        keyAdded="temp",
        subset=False
    )

    if not ls_shm:
        adata.X = adata.X.A if ss.issparse(adata.X) else adata.X
    else:
        (shmName, shape, dtype) = ls_shm
        shm = SharedMemory(shmName)
        mtxInShm = np.ndarray(shape=shape, dtype=dtype, buffer=shm.buf)
        ls_keepVar = mtxInShm[adata.obs['keep']].sum(0) > minCellCounts
        ad_parsed.X = mtxInShm
        ad_parsed = adata[adata.obs['keep'], ls_keepVar]

    df_diffxpyResult = testTwoSample(
        ad_parsed,
        "temp",
        batchLabel,
        quickScale,
        sizeFactor,
        constrainModel=constrainModel,
    )
    return df_diffxpyResult