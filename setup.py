from setuptools import setup, find_packages

setup(
    name="srltcp",
    version="1.0.0",
    description="SRLTCP client",
    packages=find_packages(exclude=["android", "android.*"]),
    python_requires=">=3.8",
    extras_require={
        "dev": ["pytest>=7.0.0", "ruff>=0.1.0"],
    },
)
