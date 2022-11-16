# causalpaca
Creating an Ensemble Climate Emulator that can incorporate causality.

## Best coding practices
Here are few best practices to keep in mind. It will help us to maintain a more consistent code and an overall better code :).

- __Git__: Try to make small commits with meaningful messages. When adding a new functionnality, make a pull request and add a short description. You can also assign the revision to other contributors.

- __Continuous integration__: When pushing your code, CircleCI routines will make sure that you follow the PEP8 guidelines and you can also run unit tests from `tests.py`. It is possible to change CircleCI configurations in `.circleci/config.yml`. To see the results: `https://app.circleci.com/`.

- __Comments__: For function's docstrings comments, let's use the google format with python annotation (as "PEP 484 type annotations" in https://www.sphinx-doc.org/en/master/usage/extensions/example_google.html).

## Installation Emulator Part

Problem from the emulator part: We need the package xesmf. This is best installed
with conda (some of the dependencies are a real pain to build from source).

So, right now the installation is:
  conda create --name causalpaca python=3.9
  conda activate causalpaca
  conda install -c conda-forge xesmf
  pip install -r requirements2.txt

[Let's stay with that until we are forced to use something else because we cannot use conda everywhere?]

Question: Can I somehow move the xesmf package into the venv?

Beforehand, the installation was like that:

python -m venv env
source env/bin/activate
python -m pip install --upgrade pip
pip install wheel setuptools
pip install -r requirements2.txt
