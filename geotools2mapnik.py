#!/usr/bin/env python

import os
import sys
import optparse
import tempfile
import shlex
import mapnik2 as mapnik
from lxml import etree
from lxml import objectify

FIX_HEX = False

def proj4_from_osr(shp_dir):
    from osgeo import osr
    srs = osr.SpatialReference()
    try:
        prj_file = open(shp_dir+ '.prj','r').read()
    except IOError:
        return None
    srs.SetFromUserInput(prj_file)
    proj4 = srs.ExportToProj4()
    if not proj4:
        #ERROR 6: No translation for Lambert_Conformal_Conic to PROJ.4 format is known.
        srs.MorphFromESRI()
    proj4 = srs.ExportToProj4()
    if proj4:
        return proj4
    else:
        return None

def rgb_to_hex(triplet):
    return '#%02x%02x%02x' % triplet

def is_number(s):
    """ Test if the value can be converted to a number.
    """
    try:
        if str(s).startswith('0'):
            return False
        float(s)
        return True
    except ValueError:
        return False

def name2expr(sym):
    name = sym.attrib['name']
    expression = '[%s]' % name
    sym.attrib['name'] = expression    

def fixup_pointsym(sym):
    if sym.attrib.get('width'):
        sym.attrib.pop('width')
    if sym.attrib.get('height'):
        sym.attrib.pop('height')
    #if sym.attrib.get('type'):
    #    sym.attrib.pop('type')

def get_cap(cap):
    if cap == 'square':
        return mapnik.line_cap.SQUARE_CAP
    if cap == 'flat':
        return mapnik.line_cap.BUTT_CAP
    else:
        return mapnik.line_cap.ROUND_CAP

def get_join(join):
    if join == 'bevel':
        return mapnik.line_join.BEVEL_JOIN
    elif join == 'round':
        return mapnik.line_join.ROUND_JOIN
    else:
        return mapnik.line_join.MITER_JOIN

def _ogc_filter_to_expression(prop):
    if 'And' in prop.tag:
        return ' and '.join(map(_ogc_filter_to_expression,
                                prop.iterchildren()))
    elif 'Or' in prop.tag:
        return ' or '.join(map(_ogc_filter_to_expression,
                                prop.iterchildren()))
    elif 'PropertyIsGreaterThan' in prop.tag:
        return _compile_bin_op('>', prop.iterchildren())
    elif 'PropertyIsLessThan' in prop.tag:
        return _compile_bin_op('<', prop.iterchildren())
    elif 'PropertyIsEqualTo' in prop.tag:
        return _compile_bin_op('=', prop.iterchildren())
    elif 'PropertyIsNotEqualTo' in prop.tag:
        return _compile_bin_op('!=', prop.iterchildren())
    elif 'PropertyIsBetween' in prop.tag:
        name = prop.PropertyName
        cql_lo = _compile_bin_op('>', [name, prop.LowerBoundary.Literal])
        cql_hi = _compile_bin_op('<', [name, prop.UpperBoundary.Literal])
        return cql_lo + 'and ' + cql_hi
        
    raise AssertionError(etree.tounicode(prop, pretty_print=True))

def _compile_bin_op(operator, arg_nodes):
    ops = map(_translate_literal_or_property_name, arg_nodes)
    assert len(ops)==2
    return "%s %s %s" % (ops[0], operator, ops[1])


def _translate_literal_or_property_name(e):
    if 'Literal' in e.tag:
        if is_number(e.text):
            return "%s"%e.text
        else:
            return "'%s'"%e.text
    elif 'PropertyName' in e.tag:
        return "[%s]" % e.text
    raise AssertionError

def ogc_filter_to_mapnik(ogc_filter):
    cql = _ogc_filter_to_expression(ogc_filter.getchildren()[0])
    if mapnik.mapnik_version() >= 800:
        return mapnik.Expression(str(cql))
    else:
        return mapnik.Filter(str(cql))

def stroke_to_mapnik(stroke):
    m_stroke = mapnik.Stroke()
    for css in stroke.CssParameter:
        if css.get('name') == 'stroke':
            m_stroke.color = mapnik.Color(css.text)
        elif css.get('name') == 'stroke-width':
            m_stroke.width = float(css.text)
        elif css.get('name') == 'stroke-opacity':
            m_stroke.opacity = float(css.text)
        elif css.get('name') == 'stroke-dasharray':
            m_stroke.add_dash(*map(float,css.text.strip().split(' ')))
        elif css.get('name') == 'stroke-linecap':
            m_stroke.line_cap = get_cap(css.text)
        elif css.get('name') == 'stroke-join':
            m_stroke.line_join = get_join(css.text)
        elif css.get('name') == 'stroke-linejoin':
            m_stroke.line_join = get_join(css.text)
        elif css.get('name') == 'stroke-dashoffset':
            m_stroke.dash_offset = float(css.text)
        else:
            raise Exception('unhanded: ' + css.get('name'))
    return m_stroke
    
def fix_colors(tree):
    if hasattr(tree,'Style'):
        for style in tree.Style:
            if len(style.Rule):
                for rule in style.Rule:
                    for child in rule.iterchildren():
                        if child.tag.endswith('Symbolizer'):
                            items = child.items()
                            for i in items:
                                if len(i) == 2:
                                    name,value = i
                                    if str(value).startswith('rgb('):
                                        c = mapnik.Color(value)
                                        triplet = (c.r,c.g,c.b) 
                                        child.set(name,rgb_to_hex(triplet))


def main(root,**options):
    m = mapnik.Map(1,1)
    
    idx = 0
    
    layers = []
    if hasattr(root,'NamedLayer'):
        layers.extend(root.NamedLayer)
    if hasattr(root,'UserLayer'):
        layers.extend(root.UserLayer)
    for layer in layers:
        lyr = mapnik.Layer(str(getattr(layer,'Name',None) or 'Layer'))
        datasource = options.get('datasource')
        if datasource and datasource.endswith('shp'):
            shp_dir = os.path.abspath(datasource).split('.shp')[0]
            name = datasource.split('.shp')[0]
            lyr.datasource = mapnik.Shapefile(file=shp_dir)
            if options.get('srid'):
                lyr.srs = '+init=epsg:%s' % options.get('srid')
                m.srs = lyr.srs
            else:
                srs = proj4_from_osr(shp_dir)
                if srs:
                    lyr.srs = srs
    
        for user_style in layer.UserStyle:
            for feature_style in user_style.FeatureTypeStyle:
                m_sty = mapnik.Style()
                # TODO = Styles should have title,abstract, etc...
                sty_name = getattr(feature_style,'Name',None)
                if not sty_name:
                    sty_name = '%s %s' % (lyr.name,str(idx))
                sty_name = str(sty_name)
    
                for rule in feature_style.Rule:
                    #print rule.get_childen()
                    m_rule = mapnik.Rule(str(getattr(rule,'Name','')))
                    ogc_filter = rule.find("{%s}Filter" % rule.nsmap['ogc'])
                    if ogc_filter is not None:
                        # TODO - support ogc:And and oc:Or
                        m_rule.filter = ogc_filter_to_mapnik(ogc_filter)
                    else:
                        if hasattr(rule,'ElseFilter'):
                              m_rule.set_else(True)
                    if hasattr(rule,'MaxScaleDenominator'):
                        m_rule.max_scale = float(rule.MaxScaleDenominator)
                    if hasattr(rule,'MinScaleDenominator'):
                        m_rule.min_scale = float(rule.MinScaleDenominator)                    
                    if hasattr(rule,'LineSymbolizer'):
                        stroke = rule.LineSymbolizer.Stroke
                        m_stroke = stroke_to_mapnik(stroke)
                        m_rule.symbols.append(mapnik.LineSymbolizer(m_stroke))
                    if hasattr(rule,'PolygonSymbolizer'):
                        m_poly = mapnik.PolygonSymbolizer()
                        if hasattr(rule.PolygonSymbolizer,'Fill'):
                            fill = rule.PolygonSymbolizer.Fill
                            for css in fill.CssParameter:
                                if css.get('name') == 'fill':
                                    m_poly.fill = mapnik.Color(css.text)
                                elif css.get('name') == 'fill-opacity':
                                    m_poly.opacity = float(css.text)
                                else:
                                    raise Exception('unhanded: ' + css.get('name'))
                        if hasattr(rule.PolygonSymbolizer,'Stroke'):
                            stroke = rule.PolygonSymbolizer.Stroke
                            m_stroke = stroke_to_mapnik(stroke)
                            m_rule.symbols.append(mapnik.LineSymbolizer(m_stroke))
                            
                        m_rule.symbols.append(m_poly)
                    if hasattr(rule,'PointSymbolizer'):
                        #fill = rule.PolygonSymbolizer.Fill
                        #m_point = point_to_mapnik(point)
                        # TODO
                        m_rule.symbols.append(mapnik.PointSymbolizer())
                    if hasattr(rule,'TextSymbolizer'):
                        text = rule.TextSymbolizer
                        name = text.Label.find("{%s}PropertyName" % rule.nsmap['ogc'])
                        if not name and hasattr(text,'Label'):
                            name = shlex.split(str(text.Label))[0]

                        face_name = '[%s]' % text.Font

                        face_name = 'DejaVu Sans Book'
                        size = 10
                        for css in text.Font.CssParameter:
                           if css.get('name') == 'font-family':
                               face_name = css.text
                           elif css.get('name') == 'font-size':
                               size = int(float(css.text))
                        color = mapnik.Color('black')
                        for css in text.Fill.CssParameter:
                            if css.get('name') == 'fill':
                                color = mapnik.Color(css.text)
                        m_text = mapnik.TextSymbolizer(mapnik.Expression('['+str(name)+']'),str(face_name),int(size),color)
                        if hasattr(text,'LabelPlacement'):
                            if hasattr(text.LabelPlacement,'LinePlacement'):
                                m_text.label_placement = mapnik.label_placement.LINE_PLACEMENT
                        if hasattr(text,'Halo'):
                            h = text.Halo
                            if hasattr(h,'Radius'):
                                m_text.halo_radius = float(h.Radius)
                            if hasattr(h,'Fill'):
                                for css in h.Fill.CssParameter:
                                    if css.get('name') == 'fill':
                                        m_text.halo_fill = mapnik.Color(css.text)
                            
                        m_rule.symbols.append(m_text)

                    m_sty.rules.append(m_rule)
                
                lyr.styles.append(sty_name)
                m.append_style(sty_name,m_sty)
                idx+= 1
                
    m.layers.append(lyr)
    if FIX_HEX:
        (handle, path) = tempfile.mkstemp(suffix='.xml', prefix='geotools2mapnik-')
        os.close(handle)
        open(path,'w').write(mapnik.save_map_to_string(m))
        tree = objectify.parse(path)
        fix_colors(tree)
        print etree.tostring(tree)#,pretty_print=True)
    else:
        print mapnik.save_map_to_string(m)

if __name__ == '__main__':
    parser = optparse.OptionParser(usage="""geotools2mapnik.py <sld.xml> [shapefile] [OPTIONS]""")

    parser.add_option('--srid',
        type='int', dest='srid',
        help='Provide an epsg code for the srs')

    parser.add_option('-d','--datasource',
        type='str', dest='datasource',
        help='Provide an path to a shapefile datasource')
                    
    (options, args) = parser.parse_args()
    
    if len(args) < 1:
      sys.exit('provide path to an sld file')

    xml = args[0]
            
    tree = objectify.parse(xml)
    root = tree.getroot()
    
    main(root,**options.__dict__)
