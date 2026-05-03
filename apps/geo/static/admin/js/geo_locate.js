/**
 * geo_locate.js
 * Adds a "📍 Use My Location" button to every Django GIS OpenLayers map widget.
 *
 * Strategy (handles plain HTTP where Chrome blocks GPS):
 *   1. Try browser GPS (navigator.geolocation) — works on HTTPS / localhost
 *   2. If denied/unavailable → silently fall back to IP geolocation via ipapi.co
 *      (city-level accuracy, ~1–5 km, but enough to center the campus map)
 *
 * Django's OpenLayersWidget stores each map instance as:
 *   window.geodjango_<fieldname>  →  { map: OLMapInstance, ... }
 * The map div id is:  id_<fieldname>_map
 */

(function () {
    'use strict';

    /* ------------------------------------------------------------------ */
    /*  Helpers                                                             */
    /* ------------------------------------------------------------------ */

    function moduleNameFromMapId(mapDivId) {
        var name = mapDivId;
        if (name.startsWith('id_')) { name = name.slice(3); }
        if (name.endsWith('_map'))  { name = name.slice(0, -4); }
        return 'geodjango_' + name;
    }

    /**
     * Pan the OL map to lat/lng and drop a blue dot marker.
     * @param {string} moduleName  – window key for the Django map widget
     * @param {number} lat
     * @param {number} lng
     * @param {boolean} approximate – if true, show orange "approx" marker
     */
    function panMapTo(moduleName, lat, lng, approximate) {
        var geoModule = window[moduleName];
        if (!geoModule || !geoModule.map) { return false; }

        var olMap = geoModule.map;
        var view  = olMap.getView();
        var ol    = window.ol;

        /* WGS-84 → Web Mercator */
        var coords;
        if (ol && ol.proj && ol.proj.fromLonLat) {
            coords = ol.proj.fromLonLat([lng, lat]);
        } else {
            var x = lng * 20037508.34 / 180;
            var y = Math.log(Math.tan((90 + lat) * Math.PI / 360))
                        / (Math.PI / 180) * 20037508.34 / 180;
            coords = [x, y];
        }

        /* Fly to location */
        view.animate({
            center:   coords,
            zoom:     approximate ? 14 : 17,   // less zoom for IP location
            duration: 900
        });

        /* Remove previous location layer */
        olMap.getLayers().getArray()
            .filter(function (l) { return l.get('name') === 'user-location'; })
            .forEach(function (l) { olMap.removeLayer(l); });

        /* Drop marker */
        if (ol && ol.Feature && ol.geom && ol.style && ol.layer && ol.source) {
            var dotColor  = approximate ? '#f5a623' : '#4285f4';
            var ringColor = approximate ? 'rgba(245,166,35,0.20)' : 'rgba(66,133,244,0.20)';
            var ringEdge  = approximate ? 'rgba(245,166,35,0.60)' : 'rgba(66,133,244,0.60)';

            var dot = new ol.Feature({ geometry: new ol.geom.Point(coords) });
            dot.setStyle(new ol.style.Style({
                image: new ol.style.Circle({
                    radius: 9,
                    fill:   new ol.style.Fill({ color: dotColor }),
                    stroke: new ol.style.Stroke({ color: '#ffffff', width: 2.5 })
                })
            }));

            var ring = new ol.Feature({ geometry: new ol.geom.Point(coords) });
            ring.setStyle(new ol.style.Style({
                image: new ol.style.Circle({
                    radius: 18,
                    fill:   new ol.style.Fill({ color: ringColor }),
                    stroke: new ol.style.Stroke({ color: ringEdge, width: 1.5 })
                })
            }));

            var layer = new ol.layer.Vector({
                source: new ol.source.Vector({ features: [ring, dot] }),
                zIndex: 999
            });
            layer.set('name', 'user-location');
            olMap.addLayer(layer);
        }

        return true;
    }

    /* ------------------------------------------------------------------ */
    /*  IP-based fallback (works on HTTP, ~city accuracy)                  */
    /* ------------------------------------------------------------------ */
    function locateByIP(moduleName, onDone, onFail) {
        fetch('https://ipapi.co/json/')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.latitude && data.longitude) {
                    onDone(data.latitude, data.longitude);
                } else {
                    onFail('IP geolocation returned no coordinates.');
                }
            })
            .catch(function () {
                onFail('IP geolocation request failed (check network).');
            });
    }

    /* ------------------------------------------------------------------ */
    /*  Inject button into one map container div                           */
    /* ------------------------------------------------------------------ */
    function injectButton(mapDiv) {
        if (mapDiv.querySelector('.geo-locate-btn')) { return; }

        var moduleName = moduleNameFromMapId(mapDiv.id);

        /* -- Button -- */
        var btn = document.createElement('button');
        btn.type      = 'button';
        btn.className = 'geo-locate-btn';
        btn.title     = 'Centre map on your current location';
        btn.innerHTML = '&#128205; Use My Location';

        Object.assign(btn.style, {
            position:     'absolute',
            top:          '10px',
            right:        '10px',
            zIndex:       '1000',
            background:   '#ffffff',
            border:       '2px solid #0078d4',
            borderRadius: '6px',
            padding:      '7px 14px',
            cursor:       'pointer',
            fontSize:     '13px',
            fontWeight:   '600',
            color:        '#0078d4',
            boxShadow:    '0 2px 8px rgba(0,0,0,0.25)',
            transition:   'background 0.15s, color 0.15s',
        });

        btn.addEventListener('mouseenter', function () {
            if (!btn.disabled) {
                btn.style.background = '#0078d4';
                btn.style.color      = '#ffffff';
            }
        });
        btn.addEventListener('mouseleave', function () {
            if (!btn.disabled) {
                btn.style.background = '#ffffff';
                btn.style.color      = '#0078d4';
            }
        });

        mapDiv.style.position = 'relative';
        mapDiv.appendChild(btn);

        /* -- State helpers -- */
        function setLoading(label) {
            btn.innerHTML = '&#8987; ' + label;
            btn.disabled  = true;
            btn.style.background   = '#ffffff';
            btn.style.color        = '#666';
            btn.style.borderColor  = '#aaa';
        }
        function setSuccess(label, color) {
            color = color || '#34a853';
            btn.innerHTML = '&#10003; ' + label;
            btn.disabled  = false;
            btn.style.background  = '#ffffff';
            btn.style.color       = color;
            btn.style.borderColor = color;
            setTimeout(resetBtn, 3000);
        }
        function setError(msg) {
            alert(msg);
            resetBtn();
        }
        function resetBtn() {
            btn.innerHTML = '&#128205; Use My Location';
            btn.disabled  = false;
            btn.style.background  = '#ffffff';
            btn.style.color       = '#0078d4';
            btn.style.borderColor = '#0078d4';
        }

        /* -- Click -- */
        btn.addEventListener('click', function () {

            setLoading('Locating\u2026');

            /* Try GPS first */
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(

                    /* GPS success */
                    function (pos) {
                        var ok = panMapTo(moduleName, pos.coords.latitude, pos.coords.longitude, false);
                        if (ok) {
                            setSuccess('Located (GPS)');
                        } else {
                            setError('Map not ready \u2014 please try again.');
                        }
                    },

                    /* GPS denied / failed → try IP fallback */
                    function (err) {
                        /* code 1 = permission denied (typical on HTTP) */
                        setLoading('Trying IP location\u2026');
                        locateByIP(
                            moduleName,
                            function (lat, lng) {
                                var ok = panMapTo(moduleName, lat, lng, true);
                                if (ok) {
                                    setSuccess('Located \u2248 (by IP, approximate)', '#f5a623');
                                } else {
                                    setError('Map not ready \u2014 please try again.');
                                }
                            },
                            function (msg) {
                                setError(
                                    'GPS blocked (HTTP site) and IP lookup failed.\n\n' +
                                    'To enable precise GPS:\n' +
                                    '\u2022 Access the admin over HTTPS, or\n' +
                                    '\u2022 In Chrome: chrome://flags \u2192 search \u201cInsecure origins\u201d \u2192 add http://' +
                                    window.location.host
                                );
                            }
                        );
                    },

                    { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 }
                );

            } else {
                /* No geolocation API at all — go straight to IP */
                setLoading('Trying IP location\u2026');
                locateByIP(
                    moduleName,
                    function (lat, lng) {
                        var ok = panMapTo(moduleName, lat, lng, true);
                        if (ok) {
                            setSuccess('Located \u2248 (by IP)', '#f5a623');
                        } else {
                            setError('Map not ready \u2014 please try again.');
                        }
                    },
                    function () { setError('Could not determine location.'); }
                );
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /*  Find all OL map containers and inject                              */
    /* ------------------------------------------------------------------ */
    function initAll() {
        var mapDivs = document.querySelectorAll('div.olmap[id]');
        if (mapDivs.length === 0) {
            mapDivs = document.querySelectorAll('div[id$="_map"][id^="id_"]');
        }
        mapDivs.forEach(injectButton);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { setTimeout(initAll, 600); });
    } else {
        setTimeout(initAll, 600);
    }

})();
