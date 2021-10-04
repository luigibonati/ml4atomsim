# imports

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import mdtraj as md
import itertools

from tqdm import tqdm


from .io import load_dataframe
from .numerical_utils import gaussian_kde, local_minima

__all__ = ["Loader"]

"""
OUTLINE
=======
1a. Load collective variables
   - from FILE or pd.DataFrame
1b. Load descriptors (optional)
   - from FILE or pd.DataFrame
2. (optional: load trajectory and compute descriptors)
3. Identify states from FES
4. Get dataframe (CVs, descriptors, labels)
"""


class Loader:
    def __init__(
        self, colvar, descriptors=None, kbt=2.5, stride=1, _DEV=False, **kwargs
    ):
        """Prepare inputs for stateinterpreter

        Parameters
        ----------
        colvar : pandas.DataFrame or string
            collective variables
        descriptors : pandas.DataFrame or string, optional
            input features, by default None
        kbt : float, optional
            temperature [KbT], by default 2.5
        stride : int, optional
            keep data every stride, by default 1
        _DEV : bool, optional
            enable debug mode, by default False

        Examples
        --------
        Load collective variables and descriptors from file, and store them as DataFrames

        >>> folder = 'stateinterpreter/data/test-chignolin/'
        >>> colvar_file = folder + 'COLVAR'
        >>> descr_file = folder+ 'DESCRIPTORS.csv'
        >>> data = Loader(colvar_file, descr_file, kbt=2.8, stride=10)
        >>> print(f"Colvar: {data.colvar.shape}, Descriptors: {data.descriptors.shape}")
        Colvar: (105, 9), Descriptors: (105, 783)

        """
        # collective variables data
        self.colvar = load_dataframe(colvar, **kwargs)
        self.colvar = self.colvar.iloc[::stride, :]
        if _DEV:
            print(f"Collective variables: {self.colvar.values.shape}")

        # descriptors data
        if descriptors is not None:
            self.descriptors = load_dataframe(descriptors, **kwargs)
            self.descriptors = self.descriptors.iloc[::stride, :]
            if "time" in self.descriptors.columns:
                self.descriptors = self.descriptors.drop("time", axis="columns")
            if _DEV:
                print(f"Descriptors: {self.descriptors.shape}")
            assert len(self.colvar) == len(
                self.descriptors
            ), "mismatch between colvar and descriptor length."

        # save attributes
        self.kbt = kbt
        self.stride = stride
        self._DEV = _DEV

        # initialize attributes to None
        self.traj = None
        self.basins = None

    def load_trajectory(self, traj_dict):
        """ "Load trajectory with mdtraj.

        Parameters
        ----------
        traj_dict : dict
            dictionary containing trajectory and topology (optional) file

        Exampl
        """
        traj_file = traj_dict["trajectory"]
        topo_file = traj_dict["topology"] if "topology" in traj_dict else None

        self.traj = md.load(traj_file, top=topo_file, stride=self.stride)

        assert len(self.traj) == len(
            self.colvar
        ), f"length traj ({len(self.traj)}) != length colvar ({len(self.colvar)})"

    def compute_descriptors(self):
        """Compute descriptors from trajectory:
        - Dihedral angles
        - CA distances
        - Hydrogen bonds

        Raises
        ------
        KeyError
            Trajectory needs to be set beforehand.
        """
        if self.traj is None:
            raise KeyError("Trajectory not loaded. Call self.load_trajectory() first.")

        ca = self._CA_DISTANCES()
        hb = self._HYDROGEN_BONDS()
        ang = self._ANGLES()

        self.descriptors = pd.concat([ca, hb, ang], axis=1)
        if self._DEV:
            print(f"Descriptors: {self.descriptors.shape}")
        assert len(self.colvar) == len(
            self.descriptors
        ), "mismatch between colvar and descriptor length."

    def identify_states(
        self,
        selected_cvs,
        bounds,
        logweights=None,
        fes_cutoff=5,
        optimizer=None,
        optimizer_kwargs=dict(),
        memory_saver=False, 
        splits=50
    ):
        # retrieve logweights
        if logweights is None:
            if ".bias" in self.colvar.columns:
                print(
                    "WARNING: a field with .bias is present in colvar, but it is not used for the FES."
                )
        else:
            if isinstance(logweights, str):
                w = self.colvar[logweights].values
            elif isinstance(logweights, pd.DataFrame):
                w = logweights.values
            elif isinstance(logweights, np.ndarray):
                w = logweights
            else:
                raise TypeError(
                    f"{logweights}: Accepted types are 'pandas.Dataframe', 'str' or 'numpy.ndarray' "
                )
            if w.ndim != 1:
                raise ValueError(f"{logweights}: 1D array is required for logweights")

        # store selected cvs
        self.selected_cvs = selected_cvs

        # Compute fes
        self.approximate_FES(
            selected_cvs, 
            bw_method=None, 
            logweights=logweights
        )

        self.minima = local_minima(self.fes, bounds, method=optimizer, method_kwargs=optimizer_kwargs)
           

        # Assign basins and select based on FES cutoff
        self.basins = self._basin_selection(
            self.minima,
            fes_cutoff=fes_cutoff,
            memory_saver=memory_saver, 
            splits=splits
        )

    def collect_data(self, only_selected_cvs=False):
        """Prepare dataframe with: CVs, labels and descriptors

        Parameters
        ----------
        only_selected_cvs : bool, optional
            save only selected CVs for labeling

        Returns
        -------
        pandas.DataFrame
            dataset with all data

        Raises
        ------
        KeyError
            Basins labels needs to be set beforehand.
        """

        if self.basins is None:
            raise KeyError("Basins not selected. Call identify_states() first.")

        return pd.concat(
            [
                self.colvar[self.selected_cvs] if only_selected_cvs else self.colvar,
                self.basins,
                self.descriptors,
            ],
            axis=1,
        )

    def approximate_FES(
        self, collective_vars, bw_method=None, logweights=None
    ):
        """Approximate Free Energy Surface (FES) in the space of collective_vars through Gaussian Kernel Density Estimation

        Args:
            collective_vars (numpy.ndarray or pd.Dataframe): List of sampled collective variables with dimensions [num_timesteps, num_CVs]
            bounds (list of tuples): (min, max) bounds for each collective Variable
            num (int, optional): [description]. Defaults to 100.
            bw_method ('scott', 'silverman' or a scalar, optional): Bandwidth method used in GaussianKDE. Defaults to None ('scotts' factor).
            logweights (arraylike log weights, optional): [description]. Defaults to None (uniform weights).

        Returns:
            [type]: [description]
        """
        empirical_centers = self.colvar[collective_vars].to_numpy()
        self.KDE = gaussian_kde(empirical_centers)
        self.fes = lambda X: -self.kbt*self.KDE.logpdf(X)
        return self.fes

    def plot_FES(self, bounds=None, names=["Variable 1", "Variable 2"]):
        """TODO: add doc or remove?"""
        try:
            self.FES
        except NameError:
            print(
                "Free energy surface hasn't been computed. Use approximate_FES function."
            )
        else:
            pass
        sampled_positions, f = self.FES
        FES_dims = f.ndim
        if FES_dims == 1:
            fig, ax = plt.subplots(dpi=100)
            xx = sampled_positions[0]
            ax.plot(xx, f)
            ax.set_xlabel(names[0])
            ax.set_ylabel("FES [kJ/mol]")
            return (fig, ax)
        elif FES_dims == 2:
            xx = sampled_positions[0]
            yy = sampled_positions[1]

            if not bounds:
                levels = np.linspace(1, 30, 10)
            else:
                levels = np.linspace(bounds[0], bounds[1], 10)

            fig, ax = plt.subplots(dpi=100)
            cfset = ax.contourf(xx, yy, f, levels=levels, cmap="Blues")
            # Contour plot
            cset = ax.contour(xx, yy, f, levels=levels, colors="k")
            # Label plot
            ax.clabel(cset, inline=1, fontsize=10)

            cbar = plt.colorbar(cfset)

            ax.set_xlabel(names[0])
            ax.set_ylabel(names[1])
            cbar.set_label("FES [kJ/mol]")
            return (fig, ax)
        else:
            raise ValueError("Maximum number of dimensions over which to plot is 2")

    def _basin_selection(
        self, minima, fes_cutoff=5, memory_saver=False, splits=50
    ):
        positions = self.KDE.dataset
        norms = np.linalg.norm((positions[:,np.newaxis,:] - minima), axis=2)
        classes = np.argmin(norms, axis=1)
        fes_at_minima = self.fes(minima)
        ref_fes = np.asarray([fes_at_minima[idx] for idx in classes])
        # Very slow
        if memory_saver:
            chunks = np.array_split(positions, splits, axis=0)
            fes_pts = []
            if self._DEV:
                for chunk in tqdm(chunks):
                    fes_pts.append(self.fes(chunk))
            else:
                for chunk in chunks:
                    fes_pts.append(self.fes(chunk))
            fes_pts = np.hstack(fes_pts)
        else:
            fes_pts = self.fes(positions)
        mask = (fes_pts - ref_fes) < fes_cutoff
        df = pd.DataFrame(data=classes, columns=["basin"])
        df["selection"] = mask
        return df

    # DESCRIPTORS COMPUTATION

    def _CA_DISTANCES(self):
        sel = self.traj.top.select("name CA")

        pairs = [(i, j) for i, j in itertools.combinations(sel, 2)]
        dist = md.compute_distances(self.traj, pairs)

        # Labels
        label = lambda i, j: "DIST. %s%s -- %s%s" % (
            self.traj.top.atom(i),
            "s" if self.traj.top.atom(i).is_sidechain else "",
            self.traj.top.atom(j),
            "s" if self.traj.top.atom(j).is_sidechain else "",
        )

        names = [label(i, j) for (i, j) in pairs]
        df = pd.DataFrame(data=dist, columns=names)
        return df

    def _HYDROGEN_BONDS(self):
        # H-BONDS DISTANCES / CONTACTS (donor-acceptor)
        # find donors (OH or NH)
        traj = self.traj
        _DEV = self._DEV
        donors = [
            at_i.index
            for at_i, at_j in traj.top.bonds
            if ((at_i.element.symbol == "O") | (at_i.element.symbol == "N"))
            & (at_j.element.symbol == "H")
        ]
        # keep unique
        donors = sorted(list(set(donors)))
        if _DEV:
            print("Donors:", donors)

        # find acceptors (O r N)
        acceptors = traj.top.select("symbol O or symbol N")
        if _DEV:
            print("Acceptors:", acceptors)

        # lambda func to avoid selecting interaction within the same residue
        atom_residue = lambda i: str(traj.top.atom(i)).split("-")[0]
        # compute pairs
        pairs = [
            (min(x, y), max(x, y))
            for x in donors
            for y in acceptors
            if (x != y) and (atom_residue(x) != atom_residue(y))
        ]
        # remove duplicates
        pairs = sorted(list(set(pairs)))

        # compute distances
        dist = md.compute_distances(traj, pairs)
        # labels
        label = lambda i, j: "HB_DIST %s%s -- %s%s" % (
            traj.top.atom(i),
            "s" if traj.top.atom(i).is_sidechain else "",
            traj.top.atom(j),
            "s" if traj.top.atom(j).is_sidechain else "",
        )

        # basename = 'hb_'
        # names = [ basename+str(x)+'-'+str(y) for x,y in  pairs]
        names = [label(x, y) for x, y in pairs]

        df_HB_DIST = pd.DataFrame(data=dist, columns=names)

        # compute contacts
        contacts = self.contact_function(dist, r0=0.35, d0=0, n=6, m=12)
        # labels
        # basename = 'hbc_'
        # names = [ basename+str(x)+'-'+str(y) for x,y in pairs]
        label = lambda i, j: "HB_CONTACT %s%s -- %s%s" % (
            traj.top.atom(i),
            "s" if traj.top.atom(i).is_sidechain else "",
            traj.top.atom(j),
            "s" if traj.top.atom(j).is_sidechain else "",
        )
        names = [label(x, y) for x, y in pairs]
        df = pd.DataFrame(data=contacts, columns=names)
        df = df.join(df_HB_DIST)
        return df

    def _ANGLES(self):
        # DIHEDRAL ANGLES
        # phi,psi --> backbone
        # chi1,chi2 --> sidechain

        values_list = []
        names_list = []

        for kind in ["phi", "psi", "chi1", "chi2"]:
            names, values = self._get_dihedrals(kind, sincos=True)
            names_list.extend(names)
            values_list.extend(values)

        df = pd.DataFrame(data=np.asarray(values_list).T, columns=names_list)
        return df

    def _get_dihedrals(self, kind="phi", sincos=True):
        traj = self.traj
        # retrieve topology
        table, _ = traj.top.to_dataframe()

        # prepare list for appending
        dihedrals = []
        names, values = [], []

        if kind == "phi":
            dihedrals = md.compute_phi(traj)
        elif kind == "psi":
            dihedrals = md.compute_psi(traj)
        elif kind == "chi1":
            dihedrals = md.compute_chi1(traj)
        elif kind == "chi2":
            dihedrals = md.compute_chi2(traj)
        else:
            raise KeyError("supported values: phi,psi,chi1,chi2")

        idx_list = dihedrals[0]
        for i, idx in enumerate(idx_list):
            # find residue id from topology table
            # res = table['resSeq'][idx[0]]
            # name = 'dih_'+kind+'-'+str(res)
            res = table["resName"][idx[0]] + table["resSeq"][idx[0]].astype("str")
            name = "BACKBONE " + kind + " " + res
            if "chi" in kind:
                name = "SIDECHAIN " + kind + " " + res
            names.append(name)
            values.append(dihedrals[1][:, i])
            if sincos:
                # names.append('cos_'+kind+'-'+str(res))
                name = "BACKBONE " + "cos_" + kind + " " + res
                if "chi" in kind:
                    name = "SIDECHAIN " + "cos_" + kind + " " + res
                names.append(name)
                values.append(np.cos(dihedrals[1][:, i]))

                # names.append('sin_'+kind+'-'+str(res))
                name = "BACKBONE " + "sin_" + kind + " " + res
                if "chi" in kind:
                    name = "SIDECHAIN " + "sin_" + kind + " " + res
                names.append(name)
                values.append(np.sin(dihedrals[1][:, i]))
        return names, values

    def contact_function(self, x, r0=1.0, d0=0, n=6, m=12):
        # (see formula for RATIONAL) https://www.plumed.org/doc-v2.6/user-doc/html/switchingfunction.html
        return (1 - np.power(((x - d0) / r0), n)) / (1 - np.power(((x - d0) / r0), m))
