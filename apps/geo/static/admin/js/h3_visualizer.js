(function () {
    'use strict';

    var RESOLUTION = 9;
    var h3Layer = null;

    function getH3() {
        return window.h3;
    }

    function renderHexagons(map) {
        var h3 = getH3();
        if (!h3 || !map) return;

        var textarea = map._container && document.querySelector('textarea.django-leaflet-raw-textarea');
        if (!textarea || !textarea.value) {
            if (h3Layer) { map.removeLayer(h3Layer); h3Layer = null; }
            return;
        }

        var geojson;
        try { geojson = JSON.parse(textarea.value); } catch (e) { return; }

        function extractCoords(geom) {
            if (!geom) return [];
            if (geom.type === 'Polygon') return geom.coordinates[0].map(function (c) { return [c[1], c[0]]; });
            if (geom.type === 'MultiPolygon') {
                var all = [];
                geom.coordinates.forEach(function (poly) {
                    poly[0].forEach(function (c) { all.push([c[1], c[0]]); });
                });
                return all;
            }
            return [];
        }

        var coords = extractCoords(geojson);
        if (coords.length < 3) {
            if (h3Layer) { map.removeLayer(h3Layer); h3Layer = null; }
            return;
        }

        try {
            var cells = h3.polygonToCells(coords, RESOLUTION);
            if (h3Layer) map.removeLayer(h3Layer);

            var hexFeatures = cells.map(function (cell) {
                var boundary = h3.cellToBoundary(cell);
                var latlngs = boundary.map(function (b) { return [b[0], b[1]]; });
                return L.polygon(latlngs, {
                    color: 'rgba(66, 133, 244, 0.8)',
                    fillColor: 'rgba(66, 133, 244, 0.1)',
                    weight: 2,
                    opacity: 0.8,
                    fillOpacity: 0.1
                });
            });

            h3Layer = L.layerGroup(hexFeatures).addTo(map);
        } catch (e) {
            console.error('H3 Visualization failed', e);
        }
    }

    function init() {
        var mapDivs = document.querySelectorAll('div[id$="-div-map"]');
        mapDivs.forEach(function (div) {
            var mapId = div.id.slice(0, -9) + '-map';
            var map = window['leafletmap' + mapId];
            if (!map) return;

            var textarea = document.querySelector('#' + div.id.replace('-div-map', ''));
            if (!textarea) return;

            var onChange = function () { setTimeout(function () { renderHexagons(map); }, 200); };

            textarea.addEventListener('change', onChange);

            var drawContainer = map._container.querySelector('.leaflet-draw');
            if (drawContainer) {
                map.on('draw:created', onChange);
                map.on('draw:edited', onChange);
                map.on('draw:deleted', onChange);
            }

            setTimeout(function () { renderHexagons(map); }, 1000);
        });
    }

    if (!window.h3) {
        var script = document.createElement('script');
        script.src = 'https://unpkg.com/h3-js@4.1.0';
        script.onload = function () {
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', function () { setTimeout(init, 1000); });
            } else {
                setTimeout(init, 1000);
            }
        };
        document.head.appendChild(script);
    } else {
        setTimeout(init, 1000);
    }
})();
