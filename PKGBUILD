# Maintainer: zenwattage <your-email@example.com>
pkgname=earthnet
pkgver=0.1.0
pkgrel=1
pkgdesc='A floating translucent 3D Earth globe in your terminal with live internet-connection trace arcs'
arch=('any')
url='https://github.com/zenwattage/Earthnet'
license=('MIT')
depends=('python')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
optdepends=('python-numpy: HD sixel renderer'
            'python-pillow: HD sixel renderer')
source=("$pkgname-$pkgver.tar.gz::https://github.com/zenwattage/Earthnet/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')
options=(!emptydirs)

build() {
	cd "$srcdir/Earthnet-$pkgver"
	python -m build --wheel --no-isolation
}

package() {
	cd "$srcdir/Earthnet-$pkgver"
	python -m installer --destdir="$pkgdir" dist/*.whl
	install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}