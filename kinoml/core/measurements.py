from typing import Union, Iterable

import numpy as np

from .conditions import AssayConditions
from .systems import System


class BaseMeasurement:
    """
    We will have several subclasses depending on the experiment.
    They will also provide loss functions tailored to it.

    Values of the measurement can have more than one replicate. In fact,
    single replicates are considered a specific case of a multi-replicate.

    Parameters:
        values: The numeric measurement(s)
        conditions: Experimental conditions of this measurement
        system: Molecular entities measured, contained in a System object
        strict: Whether to perform sanity checks at initialization.

    !!! todo
        Investigate possible uses for `pint`
    """

    def __init__(
        self,
        values: Union[float, Iterable[float]],
        conditions: AssayConditions,
        system: System,
        errors: Union[float, Iterable[float]] = np.nan,
        groups: set = None,
        strict: bool = True,
        metadata: dict = None,
        **kwargs,
    ):
        self._values = np.reshape(values, (1,))
        self._errors = np.reshape(errors, (1,))
        self.conditions = conditions
        self.system = system
        self.groups = groups or set()
        self.metadata = metadata or {}
        if strict:
            self.check()

    @property
    def values(self):
        return self._values

    @property
    def errors(self):
        return self._errors

    @classmethod
    def observation_model(cls, backend="pytorch"):
        """
        The observation_model function must be defined Measurement type, in the appropriate
        subclass. It dispatches to underlying static methods, suffixed by the
        backend (e.g. `_observation_model_pytorch`, `_observation_model_tensorflow`). These methods are
        _static_, so they do not have access to the class. This is done on purpose
        for composability of modular observation_model functions. The parent DatasetProvider
        classes will request just the function (and not the computed value), and
        will pass the needed variables. The signature is, hence, undefined.

        There are some standardized keyword arguments we use by convention, though:

        - `values`
        - `errors`
        """
        return cls._observation_model(backend=backend)

    @classmethod
    def _observation_model(cls, backend="pytorch", type_=None):
        try:
            method = getattr(cls, f"_observation_model_{backend}")
        except AttributeError:
            msg = f"Observation model for backend `{backend}` is not available for `{cls}` types"
            raise NotImplementedError(msg)
        else:
            return method

    @staticmethod
    def _observation_model_pytorch(*args, **kwargs):
        raise NotImplementedError("Implement in your subclass!")

    @staticmethod
    def _observation_model_xgboost(*args, **kwargs):
        raise NotImplementedError("Implement in your subclass!")

    def check(self):
        """
        Perform some checks for valid values
        """

    def __eq__(self, other):
        return (
            (self.values == other.values).all()
            and self.conditions == other.conditions
            and self.system == other.system
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} values={self.values} conditions={self.conditions!r} system={self.system!r}>"


class PercentageDisplacementMeasurement(BaseMeasurement):

    r"""
    Measurement where the value(s) must be percentage(s) of displacement.

    For the percent displacement measurements available from KinomeScan, we make the assumption (see JDC's notes) that

    $$
    D([I]) \approx \frac{1}{1 + \frac{K_d}{[I]}}
    $$

    For KinomeSCAN assays, all assays are usually performed at a single molar concentration, $ [I] \sim 1 \mu M $.

    We therefore define the following function:

    $$
    \mathbf{F}_{KinomeScan}(\Delta g, [I]) = \frac{1}{1 + \frac{exp[\Delta g] * 1[M]}{[I]}}.
    $$
    """

    def check(self):
        super().check()
        assert (0 <= self.values <= 100).all(), "One or more values are not in [0, 100]"

    @staticmethod
    def _observation_model_pytorch(dG_over_KT, inhibitor_conc=1, **kwargs):
        # FIXME: this might be upside down -- check!
        import torch

        return 100 * (1 - 1 / (1 + 1 / torch.exp(dG_over_KT)))

    @staticmethod
    def _observation_model_xgboost(dG_over_KT, dmatrix, inhibitor_conc=1, **kwargs):
        # TODO
        raise NotImplementedError


class pIC50Measurement(BaseMeasurement):

    r"""
    Measurement where the value(s) come from pIC50 experiments

    We use the Cheng Prusoff equation here.

    The [Cheng Prusoff](https://en.wikipedia.org/wiki/IC50#Cheng_Prusoff_equation) equation states the following relationship

    \begin{equation}
    K_i = \frac{IC50}{1+\frac{[S]}{K_m}}
    \end{equation}

    We make the following assumption (which will be relaxed in the future)
    $K_i \approx K_d$

    Under this assumptions, the Cheng-Prusoff equation becomes
    $$
    IC50 \approx {1+\frac{[S]}{K_m}} * K_d
    $$

    We define the following function
    $$
    \mathbf{F}_{IC_{50}}(\Delta g) = \Big({1+\frac{[S]}{K_m}}\Big) * \mathbf{F}_{K_d}(\Delta g) = \Big({1+\frac{[S]}{K_m}}\Big) * exp[\Delta g] * C[M].
    $$

    Given IC50 values given in molar units, we obtain pI50 values in molar units using the tranformation
    $$
    pIC50 [M] = -log_{10}(IC50[M])
    $$

    Finally the observation model for pIC50 values is
    $$
    \mathbf{F}_{pIC_{50}}(\Delta g) = - \frac{\Delta g + \ln\Big(\big(1+\frac{[S]}{K_m}\big)*C\Big)}{\ln(10)}.
    $$
    """

    @staticmethod
    def _observation_model_pytorch(
        dG_over_KT, substrate_conc=1e-6, michaelis_constant=1, inhibitor_conc=1, **kwargs
    ):
        import torch
        import numpy

        return (
            -(dG_over_KT + numpy.log((1 + substrate_conc / michaelis_constant) * inhibitor_conc))
            * 1
            / numpy.log(10)
        )

    @staticmethod
    def _observation_model_xgboost(
        dG_over_KT, dmatrix, substrate_conc=1e-6, michaelis_constant=1, inhibitor_conc=1, **kwargs
    ):
        """
        In XGBoost, observation models also application the loss function. In this specific case,
        MSE is applied and differentiated (twice) to provide the gradients and hessian matrices.

        $$
        loss = 1/2 * (observation_pIC50(preds)-labels)^2
        $$

        Parameters:
            dmatrix : xgboost.DMatrix
                Passed automatically by the xgboost loop

        """
        import numpy as np

        LN10 = np.log(10)

        labels = dmatrix.get_label()
        constant = np.log((1 + substrate_conc / michaelis_constant) * inhibitor_conc) / LN10

        grad = (labels + dG_over_KT / LN10 + constant) * 1 / LN10
        hess = np.full(grad.shape, 1 / LN10 ** 2)

        return grad, hess

    def check(self):
        super().check()
        msg = f"Values for {self.__class__.__name__} are expected to be in the [0, 15] range."
        assert (0 <= self.values <= 15).all(), msg


class pKiMeasurement(BaseMeasurement):

    r"""
    Measurement where the value(s) come from K_i_ experiments

    We make the assumption that $K_i \approx K_d$ and therefore $\mathbf{F}_{pK_i} = \mathbf{F}_{pK_d}$.
    """

    @staticmethod
    def _observation_model_pytorch(dG_over_KT, inhibitor_conc=1, **kwargs):
        import torch
        import numpy

        return -(dG_over_KT + numpy.log(inhibitor_conc)) * 1 / numpy.log(10)

    @staticmethod
    def _observation_model_xgboost(dG_over_KT, dmatrix, inhibitor_conc=1, **kwargs):
        # TODO
        raise NotImplementedError

    def check(self):
        super().check()
        msg = f"Values for {self.__class__.__name__} are expected to be in the [0, 15] range."
        assert (0 <= self.values <= 15).all(), msg


class pKdMeasurement(BaseMeasurement):

    r"""
    Measurement where the value(s) come from Kd experiments

    We define the following physics-based function

    $$
    \mathbf{F}_{pK_d}(\Delta g) = - \frac{\Delta g + \ln(C)}{\ln(10)},
    $$
    where C given in molar [M] can be adapted if measurements were undertaken at different concentrations.
    """

    @staticmethod
    def _observation_model_pytorch(dG_over_KT, inhibitor_conc=1, **kwargs):
        import torch
        import numpy

        return -(dG_over_KT + numpy.log(inhibitor_conc)) * 1 / numpy.log(10)

    @staticmethod
    def _observation_model_xgboost(dG_over_KT, dmatrix, inhibitor_conc=1, **kwargs):
        # TODO
        raise NotImplementedError

    def check(self):
        super().check()
        msg = f"Values for {self.__class__.__name__} are expected to be in the [0, 15] range."
        assert (0 <= self.values <= 15).all(), msg


def null_observation_model(arg):
    return arg
